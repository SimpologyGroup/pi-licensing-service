"""Paddle Billing: checking webhook signatures and turning subscription events into licences.

The store opens Paddle's checkout with the tier's price and `customData` (email, name, organisation, Org ID,
tier). Paddle then sends signed notifications to /webhooks/paddle:

- subscription.created / activated / updated / past_due / paused / resumed / canceled: the licence for that
  subscription is made or brought up to date (tier from the price, status, billing period). The first time, the
  buyer is emailed the licence id.
- customer.created / updated: remembers Paddle's customer id with the email, for subscriptions made without the
  store's customData.

Each event is stored once (`WebhookEvent`), so a repeat changes nothing; an event older than one already applied
to the same subscription is skipped. Data in the apps is never touched: a lapsed licence drops to the free tier.
"""

import hashlib
import hmac
import time
from datetime import datetime

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from . import services
from .models import Customer, License, Tier, WebhookEvent

PROVIDER = "paddle"

STATUS = {
    "active": License.ACTIVE,
    "trialing": License.ACTIVE,
    "past_due": License.PAST_DUE,
    "paused": License.CANCELED,  # runs to the end of the paid period, then expires
    "canceled": License.CANCELED,
}


class BadSignature(Exception):
    pass


def verify_signature(body: bytes, header: str, secret: str = None, now=None) -> None:
    """Paddle-Signature: ts=<unix time>;h1=<hex HMAC-SHA256 of "<ts>:<body>">. Raises BadSignature."""
    secret = settings.PADDLE_WEBHOOK_SECRET if secret is None else secret
    if not secret:
        raise BadSignature("PADDLE_WEBHOOK_SECRET is not set.")
    parts = {}
    for item in (header or "").split(";"):
        key, _, value = item.partition("=")
        parts.setdefault(key.strip(), []).append(value.strip())
    try:
        ts = int(parts["ts"][0])
    except (KeyError, ValueError) as exc:
        raise BadSignature("No timestamp.") from exc
    if abs((now or time.time()) - ts) > settings.PADDLE_WEBHOOK_TOLERANCE_SECONDS:
        raise BadSignature("Too old.")
    expected = hmac.new(secret.encode(), f"{ts}:".encode() + body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, h) for h in parts.get("h1", [])):
        raise BadSignature("Signature does not match.")


def parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def record(payload: dict) -> tuple:
    """Store the event once. Returns (event, is_new)."""
    data = payload.get("data") or {}
    sub_id = data.get("id", "") if str(payload.get("event_type", "")).startswith("subscription.") else ""
    return WebhookEvent.objects.get_or_create(
        provider=PROVIDER,
        event_id=str(payload.get("event_id", "")),
        defaults={
            "event_type": str(payload.get("event_type", ""))[:60],
            "occurred_at": parse_time(payload.get("occurred_at")),
            "subscription_id": sub_id,
            "payload": payload,
        },
    )


def process(event: WebhookEvent) -> str:
    """Apply one stored event; returns a short result that is saved on the event."""
    kind = event.event_type
    data = event.payload.get("data") or {}
    with transaction.atomic():
        if kind.startswith("subscription."):
            result = _subscription(event, data)
        elif kind in ("customer.created", "customer.updated"):
            result = _customer(data)
        else:
            result = "Ignored (not used)."
        # "Waiting" stays open, so it is applied when Paddle sends it again or it is processed again in the admin.
        event.processed = None if result.startswith("Waiting") else timezone.now()
        event.result = result[:300]
        event.save(update_fields=["processed", "result"])
    return result


def _customer(data: dict) -> str:
    email = data.get("email")
    if not email:
        return "Ignored: no email."
    customer = services.get_or_create_customer(email, data.get("name") or "")
    if customer.provider_customer_id != data.get("id", ""):
        customer.provider_customer_id = data.get("id", "")
        customer.save(update_fields=["provider_customer_id"])
    return f"Customer {customer.email}."


def _tier_for(data: dict):
    for item in data.get("items") or []:
        price_id = (item.get("price") or {}).get("id") or item.get("price_id")
        tier = Tier.objects.filter(provider_price_id=price_id).select_related("product").first() if price_id else None
        if tier:
            return tier
    return None


def _customer_for(data: dict):
    custom = data.get("custom_data") or {}
    if custom.get("email"):
        customer = services.get_or_create_customer(
            custom["email"], custom.get("name", ""), custom.get("org", ""), custom.get("org_id", ""))
        if data.get("customer_id") and not customer.provider_customer_id:
            customer.provider_customer_id = data["customer_id"]
            customer.save(update_fields=["provider_customer_id"])
        return customer
    if data.get("customer_id"):
        return Customer.objects.filter(provider_customer_id=data["customer_id"]).first()
    return None


def _subscription(event: WebhookEvent, data: dict) -> str:
    sub_id = data.get("id", "")
    if not sub_id:
        return "Ignored: no subscription id."
    later = WebhookEvent.objects.filter(
        provider=PROVIDER, subscription_id=sub_id, processed__isnull=False, occurred_at__gt=event.occurred_at
    ).exclude(pk=event.pk) if event.occurred_at else WebhookEvent.objects.none()
    if later.exists():
        return "Skipped: a later event for this subscription was already applied."
    tier = _tier_for(data)
    if tier is None:
        return "Ignored: price not linked to a tier (set the tier's provider price id)."
    license_ = License.objects.select_for_update().filter(
        provider=PROVIDER, provider_subscription_id=sub_id).first()
    status = STATUS.get(data.get("status"), License.ACTIVE)
    period = data.get("current_billing_period") or {}
    start, end = parse_time(period.get("starts_at")), parse_time(period.get("ends_at"))
    if license_ is None:
        customer = _customer_for(data)
        if customer is None:
            return "Waiting: no email for this subscription yet (process again after the customer event)."
        now = timezone.now()
        license_ = License.objects.create(
            customer=customer, product=tier.product, tier=tier, status=status,
            period_start=start or now, period_end=end or now,
            provider=PROVIDER, provider_subscription_id=sub_id,
        )
        transaction.on_commit(lambda: send_licence_email(license_))
        return f"Licence {license_.lid} made for {customer.email}."
    changed = []
    if license_.tier_id != tier.pk:
        license_.tier, license_.product = tier, tier.product
        changed += ["tier", "product"]
    if license_.status != License.REVOKED and license_.status != status:
        license_.status = status
        changed.append("status")
    if start and end:
        if (license_.period_start, license_.period_end) != (start, end):
            license_.period_start, license_.period_end = start, end
            changed += ["period_start", "period_end"]
    elif data.get("status") == "canceled" and data.get("canceled_at"):
        ended = parse_time(data["canceled_at"])
        if ended < license_.period_end:  # cancelled straight away: it runs only to that moment (plus grace)
            license_.period_end = ended
            changed.append("period_end")
    if changed:
        license_.save(update_fields=changed + ["updated"])
        return f"Licence {license_.lid} updated ({', '.join(changed)})."
    return f"Licence {license_.lid} unchanged."


def send_licence_email(license_: License) -> None:
    context = {
        "license": license_,
        "customer": license_.customer,
        "product": license_.product,
        "tier": license_.tier,
        "support": settings.STORE_SUPPORT_EMAIL,
        "company": settings.STORE_COMPANY,
    }
    subject = f"Your {license_.product.name} licence ({license_.tier.name})"
    send_mail(
        subject,
        render_to_string("licensing/store/licence_email.txt", context),
        settings.DEFAULT_FROM_EMAIL,
        [license_.customer.email],
        html_message=render_to_string("licensing/store/licence_email.html", context),
        fail_silently=True,
    )
