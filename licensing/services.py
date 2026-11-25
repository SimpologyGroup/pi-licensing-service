"""Licence rules.

- Each customer has at most one licence per product in use. A paid licence that is active, payment overdue or
  cancelled counts until its period end plus the grace days; after that it expires.
- With no paid licence in use the customer is on the product's free tier: its licence is issued on the spot and
  renews itself every FREE_LICENSE_DAYS. So a lapsed paid licence drops to the free tier, and no data is touched.
"""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from pi_license import build_payload, sign, wrap

from . import keystore
from .models import Activation, Certificate, Customer, License, Product, Tier


class NotFound(Exception):
    pass


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def get_product(code: str) -> Product:
    try:
        return Product.objects.get(code=code, active=True)
    except Product.DoesNotExist as exc:
        raise NotFound(f"Unknown product {code!r}.") from exc


def get_or_create_customer(email: str, name: str = "", org: str = "") -> Customer:
    email = normalise_email(email)
    customer, created = Customer.objects.get_or_create(email=email, defaults={"name": name, "org": org})
    if not created and ((name and not customer.name) or (org and not customer.org)):
        customer.name = customer.name or name
        customer.org = customer.org or org
        customer.save(update_fields=["name", "org"])
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


@transaction.atomic
def ensure_free_license(customer: Customer, product: Product, now=None) -> License:
    now = now or timezone.now()
    existing = (
        License.objects.select_for_update()
        .filter(customer=customer, product=product, tier__is_free=True)
        .exclude(status=License.REVOKED)
        .first()
    )
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


def current_license(customer: Customer, product: Product, now=None) -> License:
    """The licence in use: the newest usable paid one, else the free tier."""
    now = now or timezone.now()
    paid = (
        License.objects.filter(customer=customer, product=product, tier__is_free=False, status__in=License.USABLE)
        .select_related("tier", "product", "customer")
        .order_by("-period_end")
    )
    for license_ in paid:
        if is_usable(license_, now):
            return license_
    return ensure_free_license(customer, product, now)


def issue_certificate(license_: License, now=None) -> Certificate:
    now = now or timezone.now()
    kid, key = keystore.current()
    tier = license_.tier
    payload = build_payload(
        lid=license_.lid,
        customer_id=license_.customer.public_id,
        email=license_.customer.email,
        org=license_.customer.org,
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
    )
    return Certificate.objects.create(
        license=license_,
        kid=kid,
        plan=tier.code,
        status=license_.status,
        issued_at=now,
        expires_at=license_.period_end,
        token=sign(payload, key, kid),
    )


def current_certificate(license_: License, now=None) -> Certificate:
    """The newest certificate if it still describes the licence (same tier, status, period end and key), else a
    new one. Reusing it keeps the certificate table small when apps ask often."""
    kid, _ = keystore.current()
    latest = license_.certificates.first()
    if (
        latest
        and latest.plan == license_.tier.code
        and latest.status == license_.status
        and latest.expires_at == license_.period_end
        and latest.kid == kid
    ):
        return latest
    return issue_certificate(license_, now)


def record_activation(license_: License, app_client, account_ref: str) -> Activation:
    activation, _ = Activation.objects.update_or_create(
        license=license_, account_ref=account_ref, defaults={"app_client": app_client}
    )
    return activation


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
        "org": license_.customer.org,
        "period_start": license_.period_start.isoformat(),
        "period_end": license_.period_end.isoformat(),
        "certificate": wrap(certificate.token),
    }
