"""Proof that an app account may see and use licences.

An email can be guessed and an Org ID counts up, so neither is proof on its own. An app administrator proves a
licence email by entering the six-digit code the service emails to it (`send_code`, `check_code`); the account may
then list and use that email's licences for LICENSE_EMAIL_PROOF_HOURS. An account is linked to an organisation
(`OrgLink`) only after proving an email of that organisation, or by using one of its licences; then it may list the
organisation's licences. A licence is also proved by its certificate (the signed .pilic file sent to the buyer).

The website's key (no product) acts for the business itself and needs no proof.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
from pi_license import keys as pi_keys
from pi_license import verify

from . import keystore
from .models import Customer, EmailCode, EmailProof, License, OrgLink


class TooMany(Exception):
    """Too many codes asked for, or too many wrong codes."""


class BadCode(Exception):
    """The code is wrong, used or expired."""


class NotProven(Exception):
    """The account has not proved it may see or use this."""


def exempt(app_client) -> bool:
    return app_client.product_id is None


def _hash(email: str, code: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def send_code(app_client, account_ref: str, email: str, account_name: str = "", requested_by: str = "",
              now=None) -> EmailCode:
    """Email a new code to `email` (older codes for it and this account stop working). Sent whether or not the email
    holds licences, so the answer never tells anyone which emails do."""
    now = now or timezone.now()
    hour_ago = now - timedelta(hours=1)
    if EmailCode.objects.filter(email=email, created__gte=hour_ago).count() >= settings.LICENSE_CODES_PER_HOUR:
        raise TooMany("Too many codes were asked for this email. Please wait an hour and try again.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    with transaction.atomic():
        EmailCode.objects.filter(app_client=app_client, account_ref=account_ref, email=email, used=False).update(
            used=True)
        row = EmailCode.objects.create(app_client=app_client, account_ref=account_ref, email=email,
                                       code_hash=_hash(email, code),
                                       expires_at=now + timedelta(minutes=settings.LICENSE_CODE_MINUTES))
    product = app_client.product
    context = {
        "code": code,
        "minutes": settings.LICENSE_CODE_MINUTES,
        "product": product.name if product else "",
        "account_name": account_name or account_ref,
        "requested_by": requested_by,
        "support": settings.STORE_SUPPORT_EMAIL,
        "company": settings.STORE_COMPANY,
    }
    send_mail(
        f"Your code for {context['product']}: {code}",
        render_to_string("licensing/email_code.txt", context),
        settings.DEFAULT_FROM_EMAIL,
        [email],
        html_message=render_to_string("licensing/email_code.html", context),
        fail_silently=False,
    )
    return row


def check_code(app_client, account_ref: str, email: str, code: str, now=None) -> EmailProof:
    """Right code: the account has proved the email. Wrong codes count against the code; after
    LICENSE_CODE_TRIES it stops working and a new one is needed."""
    now = now or timezone.now()
    code = "".join((code or "").split())
    problem = None
    with transaction.atomic():  # count the wrong try, then raise outside, so the count is kept
        row = (EmailCode.objects.select_for_update()
               .filter(app_client=app_client, account_ref=account_ref, email=email, used=False,
                       expires_at__gt=now).first())
        if row is None:
            problem = BadCode("That code has expired or was replaced by a newer one. Ask for a new code.")
        elif not (code.isdigit() and len(code) == 6 and hmac.compare_digest(row.code_hash, _hash(email, code))):
            row.attempts += 1
            row.used = row.attempts >= settings.LICENSE_CODE_TRIES
            row.save(update_fields=["attempts", "used"])
            problem = (TooMany("Too many wrong codes. Ask for a new code.") if row.used else
                       BadCode("That code is not right. Please check the email and try again."))
        else:
            row.used = True
            row.save(update_fields=["used"])
            proof, _ = EmailProof.objects.update_or_create(app_client=app_client, account_ref=account_ref,
                                                           email=email, defaults={"verified_at": now})
    if problem:
        raise problem
    return proof


def has_email_proof(app_client, account_ref: str, email: str, now=None) -> bool:
    if exempt(app_client):
        return True
    now = now or timezone.now()
    since = now - timedelta(hours=settings.LICENSE_EMAIL_PROOF_HOURS)
    return EmailProof.objects.filter(app_client=app_client, account_ref=account_ref, email=email,
                                     verified_at__gte=since).exists()


def is_linked(app_client, account_ref: str, organisation) -> bool:
    if exempt(app_client):
        return True
    return OrgLink.objects.filter(app_client=app_client, account_ref=account_ref, organisation=organisation).exists()


def link(app_client, account_ref: str, organisation, now=None) -> OrgLink:
    """Link the account to an organisation it has proved an email of (or is already linked to)."""
    if not is_linked(app_client, account_ref, organisation):
        now = now or timezone.now()
        since = now - timedelta(hours=settings.LICENSE_EMAIL_PROOF_HOURS)
        emails = EmailProof.objects.filter(app_client=app_client, account_ref=account_ref,
                                           verified_at__gte=since).values_list("email", flat=True)
        if not Customer.objects.filter(email__in=list(emails), organisation=organisation).exists():
            raise NotProven("First confirm the email of someone in that organisation with a code (Find Licences "
                            "by Email), then link it.")
    link_, _ = OrgLink.objects.get_or_create(app_client=app_client, account_ref=account_ref,
                                             organisation=organisation)
    return link_


def certificate_names(certificate: str, license_: License) -> bool:
    """The certificate is genuine (signed by this service) and is for this licence."""
    try:
        found = verify(certificate, pi_keys.keys_from_jwks(keystore.jwks()), product=license_.product.code)
    except Exception:
        return False
    return found.lid == license_.lid


def may_use(license_: License, app_client, account_ref: str, certificate: str = "", now=None) -> bool:
    """The account may tie this licence to itself: it already has it, has proved the email it is held under, is
    linked to its organisation, or has its certificate."""
    from .services import relation

    if exempt(app_client) or relation(license_, app_client, account_ref) == "this":
        return True
    if has_email_proof(app_client, account_ref, license_.customer.email, now):
        return True
    org = license_.customer.organisation
    if org is not None and is_linked(app_client, account_ref, org):
        return True
    return bool(certificate) and certificate_names(certificate, license_)
