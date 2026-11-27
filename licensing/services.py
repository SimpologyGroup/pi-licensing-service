"""Licence rules.

- A paid licence that is active, payment overdue or cancelled counts until its period end plus the grace days;
  after that it expires.
- One licence, one app account: a licence is tied to the first account (in one app installation) that links or
  uploads it (`Activation`); other accounts are refused (`InUse`) until it is released in the admin. A customer
  (email) can hold several licences, e.g. one per account; the app's administrator picks which one to use.
- An account with no paid licence in use is on the product's free tier: a free licence for that account is issued
  on the spot and renews itself every FREE_LICENSE_DAYS. So a lapsed paid licence drops to the free tier, and no
  data is touched.
"""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from pi_license import build_payload, sign, wrap

from . import keystore
from .models import Activation, Certificate, Customer, License, Organisation, Product, Tier


MANUAL_PROVIDER = "manual"  # issued by hand in the admin, no payment


class NotFound(Exception):
    pass


class InUse(Exception):
    """The licence already belongs to another app account."""


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def get_product(code: str) -> Product:
    try:
        return Product.objects.get(code=code, active=True)
    except Product.DoesNotExist as exc:
        raise NotFound(f"Unknown product {code!r}.") from exc


def get_or_create_customer(email: str, name: str = "", org: str = "", org_id: str = "") -> Customer:
    """The customer for an email (new ones are made). A customer without an organisation is put in the one named
    by `org_id` (an existing Org ID), or else in a new organisation called `org`; one that has an organisation
    keeps it (change it in the admin)."""
    email = normalise_email(email)
    customer, created = Customer.objects.get_or_create(email=email, defaults={"name": name})
    changed = []
    if name and not customer.name:
        customer.name = name
        changed.append("name")
    if customer.organisation_id is None and (org_id or org):
        organisation = Organisation.objects.filter(org_id=org_id.strip().upper()).first() if org_id else None
        if organisation is None and org:
            organisation = Organisation.objects.create(name=org)
        if organisation is not None:
            customer.organisation = organisation
            changed.append("organisation")
    if changed:
        customer.save(update_fields=changed)
    return customer


def free_tier(product: Product) -> Tier:
    try:
        return product.tiers.get(is_free=True, active=True)
    except Tier.DoesNotExist as exc:
        raise NotFound(f"{product.name} has no free tier.") from exc


def grace(license_: License) -> timedelta:
    return timedelta(days=settings.LICENSE_GRACE_DAYS)


def refresh(license_: License, now=None) -> License:
    """Bring a licence's status up to date with the clock."""
    now = now or timezone.now()
    if license_.is_free:
        if license_.status == License.ACTIVE and now >= license_.period_end:
            # Free licences renew themselves for as long as the customer is with us.
            while license_.period_end <= now:
                license_.period_start = license_.period_end
                license_.period_end = license_.period_end + timedelta(days=settings.FREE_LICENSE_DAYS)
            license_.save(update_fields=["period_start", "period_end", "updated"])
    elif license_.status in License.USABLE and now > license_.period_end + grace(license_):
        license_.status = License.EXPIRED
        license_.save(update_fields=["status", "updated"])
    return license_


def is_usable(license_: License, now=None) -> bool:
    return refresh(license_, now).status in License.USABLE


def binding(license_: License):
    """The account the licence belongs to (an Activation), or None while it is not tied to one."""
    return license_.activations.select_related("app_client").first()


def relation(license_: License, app_client=None, account_ref: str = "") -> str:
    """'' = free to use, 'this' = used by the account asking, 'other' = used by another account."""
    act = binding(license_)
    if act is None:
        return ""
    return "this" if app_client is not None and account_ref and act.is_for(app_client, account_ref) else "other"


@transaction.atomic
def ensure_free_license(customer: Customer, product: Product, now=None, app_client=None,
                        account_ref: str = "") -> License:
    """The free licence to use: for an account, the one tied to it or one not tied to any account (a new one when
    every free licence of this email is used by other accounts). Without an account (website sign-up): the first."""
    now = now or timezone.now()
    candidates = list(
        License.objects.select_for_update()
        .filter(customer=customer, product=product, tier__is_free=True)
        .exclude(status=License.REVOKED)
        .order_by("created", "pk")
    )
    if account_ref:
        ranked = [x for x in candidates if relation(x, app_client, account_ref) == "this"] + [
            x for x in candidates if relation(x, app_client, account_ref) == ""]
        existing = ranked[0] if ranked else None
    else:
        free_to_use = [x for x in candidates if binding(x) is None]
        existing = (free_to_use or candidates or [None])[0]
    if existing:
        if existing.status != License.ACTIVE:
            existing.status = License.ACTIVE
            existing.save(update_fields=["status", "updated"])
        return refresh(existing, now)
    return License.objects.create(
        customer=customer,
        product=product,
        tier=free_tier(product),
        status=License.ACTIVE,
        period_start=now,
        period_end=now + timedelta(days=settings.FREE_LICENSE_DAYS),
    )


def current_license(customer: Customer, product: Product, now=None, app_client=None, account_ref: str = "") -> License:
    """The licence to use: the paid one tied to this account, else the newest usable paid one no other account uses,
    else the free tier. Without an account: the newest usable paid one (any), else the free tier."""
    now = now or timezone.now()
    paid = list(
        License.objects.filter(customer=customer, product=product, tier__is_free=False, status__in=License.USABLE)
        .select_related("tier", "product", "customer")
        .order_by("-period_end")
    )
    if account_ref:
        paid = [x for x in paid if relation(x, app_client, account_ref) == "this"] + [
            x for x in paid if relation(x, app_client, account_ref) == ""]
    for license_ in paid:
        if is_usable(license_, now):
            return license_
    return ensure_free_license(customer, product, now, app_client, account_ref)


def licenses_for_email(customer: Customer, product: Product, app_client, account_ref: str, now=None) -> list:
    """What an administrator can pick from: the email's usable licences for the product, newest first, each with
    its relation to the account asking. A free licence this account can use is always among them."""
    now = now or timezone.now()
    ensure_free_license(customer, product, now, app_client, account_ref)
    out = []
    for license_ in (License.objects.filter(customer=customer, product=product)
                     .select_related("tier", "product", "customer").order_by("tier__is_free", "-period_end")):
        if not is_usable(license_, now):
            continue
        out.append(license_)
    return out


def licenses_for_org(organisation: Organisation, product: Product, now=None) -> list:
    """The usable licences of every customer in an organisation for the product, paid first, newest first: what an
    app account linked to that organisation picks from."""
    now = now or timezone.now()
    return [x for x in License.objects.filter(customer__organisation=organisation, product=product)
            .select_related("tier", "product", "customer", "customer__organisation")
            .order_by("tier__is_free", "-period_end") if is_usable(x, now)]


def find_organisation(org_id: str):
    """The organisation with this Org ID (any case, spaces ignored), or None."""
    org_id = "".join((org_id or "").split()).upper()
    return Organisation.objects.filter(org_id=org_id).first() if org_id else None


def _bound_to(license_: License):
    """(account, installation) the certificate names: empty while the licence is not tied to an account."""
    act = binding(license_)
    return (act.account_ref, act.app_client.key_prefix) if act else ("", "")


def issue_certificate(license_: License, now=None) -> Certificate:
    now = now or timezone.now()
    kid, key = keystore.current()
    tier = license_.tier
    account, installation = _bound_to(license_)
    payload = build_payload(
        lid=license_.lid,
        customer_id=license_.customer.public_id,
        email=license_.customer.email,
        org=license_.customer.org_name,
        org_id=license_.customer.org_id,
        product=license_.product.code,
        plan=tier.code,
        limits=tier.limits,
        features=tier.features,
        subscription={
            "status": license_.status,
            "interval": tier.interval,
            "provider": license_.provider or "free",
            "id": license_.provider_subscription_id,
            "price_usd": str(tier.price_usd),
        },
        valid_from=license_.period_start,
        expires_at=license_.period_end,
        grace_days=0 if tier.is_free else settings.LICENSE_GRACE_DAYS,
        check_hours=settings.LICENSE_CHECK_HOURS,
        issued_at=now,
        account=account,
        installation=installation,
    )
    return Certificate.objects.create(
        license=license_,
        kid=kid,
        plan=tier.code,
        status=license_.status,
        issued_at=now,
        expires_at=license_.period_end,
        account_ref=f"{installation}:{account}" if account else "",
        token=sign(payload, key, kid),
    )


def current_certificate(license_: License, now=None) -> Certificate:
    """The newest certificate if it still describes the licence (same tier, status, period end and key), else a
    new one. Reusing it keeps the certificate table small when apps ask often."""
    kid, _ = keystore.current()
    latest = license_.certificates.first()
    account, installation = _bound_to(license_)
    if (
        latest
        and latest.plan == license_.tier.code
        and latest.status == license_.status
        and latest.expires_at == license_.period_end
        and latest.kid == kid
        and latest.account_ref == (f"{installation}:{account}" if account else "")
    ):
        return latest
    return issue_certificate(license_, now)


@transaction.atomic
def record_activation(license_: License, app_client, account_ref: str) -> Activation:
    """Tie the licence to this account, or confirm it already is. Raises InUse when another account has it."""
    License.objects.select_for_update().filter(pk=license_.pk).first()  # one at a time per licence
    act = binding(license_)
    if act is not None:
        if not act.is_for(app_client, account_ref):
            raise InUse("This licence is already used by another account. Each licence is for one account: "
                        "use another licence, or ask us to release it.")
        act.save(update_fields=["last_seen"])
        return act
    return Activation.objects.create(license=license_, app_client=app_client, account_ref=account_ref)


def summary(license_: License, app_client=None, account_ref: str = "") -> dict:
    """A licence without its certificate, for the list an administrator picks from."""
    tier = license_.tier
    return {
        "lid": license_.lid,
        "product": license_.product.code,
        "plan": tier.code,
        "tier": tier.name,
        "email": license_.customer.email,
        "org_id": license_.customer.org_id,
        "org": license_.customer.org_name,
        "free": tier.is_free,
        "status": license_.status,
        "status_label": license_.get_status_display(),
        "limits": tier.limits,
        "period_start": license_.period_start.isoformat(),
        "period_end": license_.period_end.isoformat(),
        "account": relation(license_, app_client, account_ref),
    }


def describe(license_: License, now=None) -> dict:
    """What the API returns for a licence, certificate included."""
    certificate = current_certificate(license_, now)
    tier = license_.tier
    return {
        "lid": license_.lid,
        "product": license_.product.code,
        "plan": tier.code,
        "tier": tier.name,
        "free": tier.is_free,
        "status": license_.status,
        "limits": tier.limits,
        "features": tier.features,
        "email": license_.customer.email,
        "org": license_.customer.org_name,
        "org_id": license_.customer.org_id,
        "period_start": license_.period_start.isoformat(),
        "period_end": license_.period_end.isoformat(),
        "certificate": wrap(certificate.token),
    }
