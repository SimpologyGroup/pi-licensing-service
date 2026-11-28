"""The public store: plans per product, a short details form, then Paddle's checkout, and the Paddle webhook.

Plans, prices and limits come from the Product and Tier tables (data, never code). A tier can be bought once it
has a Paddle price id (`provider_price_id`) and the store has a Paddle client-side token; otherwise it shows
"Coming soon".
"""

import json
import logging

from django import forms
from django.conf import settings
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import paddle, services
from .models import Product, Tier

log = logging.getLogger(__name__)

# How each limit is shown in the store. A counter not listed here is shown by its name.
LIMIT_LABELS = {
    "org_units": "Clients, business units and sites",
    "resources": "People, vehicles and tools",
    "users": "Users",
    "cashflow_lines": "Cash flow lines",
}


def store_open() -> bool:
    return bool(settings.PADDLE_CLIENT_TOKEN)


def can_buy(tier: Tier) -> bool:
    return store_open() and tier.active and not tier.is_free and bool(tier.provider_price_id)


def limit_rows(product: Product, tier: Tier) -> list:
    rows = []
    for name in product.limit_names:
        value = (tier.limits or {}).get(name)
        rows.append({"label": LIMIT_LABELS.get(name, name.replace("_", " ").capitalize()),
                     "value": "Unlimited" if value is None else f"{value:,}".replace(",", " ")})
    return rows


def plan(tier: Tier) -> dict:
    return {"tier": tier, "limits": limit_rows(tier.product, tier), "can_buy": can_buy(tier),
            "price": f"{tier.price_usd:,.0f}".replace(",", " ")}


def _base(request, **extra):
    return {"support": settings.STORE_SUPPORT_EMAIL, "company": settings.STORE_COMPANY, "open": store_open(),
            **extra}


def index(request):
    products = []
    for product in Product.objects.filter(active=True).order_by("name"):
        tiers = [plan(t) for t in product.tiers.filter(active=True).order_by("order")]
        if tiers:
            products.append({"product": product, "plans": tiers})
    return render(request, "licensing/store/index.html", _base(request, products=products))


class DetailsForm(forms.Form):
    email = forms.EmailField(label="Email", help_text="The licence is sent here. Use the email you sign in with.")
    name = forms.CharField(label="Your name", max_length=200)
    org = forms.CharField(label="Organisation", max_length=200, required=False,
                          help_text="Your company's name.")
    org_id = forms.CharField(label="Org ID (if you have one)", max_length=20, required=False,
                             help_text="Shown on your app's Subscription page, e.g. ORG-00012.")

    def clean_org_id(self):
        value = "".join(self.cleaned_data.get("org_id", "").split()).upper()
        if value and services.find_organisation(value) is None:
            raise forms.ValidationError("We don't know this Org ID. Check it, or leave it empty.")
        return value

    def clean(self):
        data = super().clean()
        if not data.get("org") and not data.get("org_id"):
            customer = services.Customer.objects.filter(
                email=services.normalise_email(data.get("email", ""))).first() if data.get("email") else None
            if not (customer and customer.organisation_id):
                self.add_error("org", "Type your organisation's name, or its Org ID.")
        return data


def buy(request, tier_code):
    tier = Tier.objects.select_related("product").filter(code=tier_code, active=True, is_free=False).first()
    if tier is None or not tier.product.active:
        raise Http404("No such plan.")
    form = DetailsForm(request.POST or None)
    checkout = None
    if request.method == "POST" and form.is_valid() and can_buy(tier):
        data = form.cleaned_data
        checkout = {
            "environment": settings.PADDLE_ENV,
            "token": settings.PADDLE_CLIENT_TOKEN,
            "items": [{"priceId": tier.provider_price_id, "quantity": 1}],
            "customer": {"email": data["email"]},
            "customData": {"email": data["email"], "name": data["name"], "org": data["org"],
                           "org_id": data["org_id"], "tier": tier.code},
            "successUrl": request.build_absolute_uri(reverse("store_thanks")) + f"?plan={tier.code}",
        }
    return render(request, "licensing/store/buy.html", _base(
        request, plan=plan(tier), product=tier.product, form=form, checkout=checkout))


def thanks(request):
    tier = Tier.objects.select_related("product").filter(code=request.GET.get("plan", "")).first()
    return render(request, "licensing/store/thanks.html", _base(request, tier=tier))


@csrf_exempt
@require_POST
def paddle_webhook(request):
    body = request.body
    try:
        paddle.verify_signature(body, request.headers.get("Paddle-Signature", ""))
    except paddle.BadSignature as exc:
        log.warning("Paddle webhook refused: %s", exc)
        return HttpResponse("Bad signature.", status=401)
    try:
        payload = json.loads(body)
    except ValueError:
        return HttpResponseBadRequest("Not JSON.")
    if not payload.get("event_id"):
        return HttpResponseBadRequest("No event id.")
    event, _ = paddle.record(payload)
    if event.processed:
        return HttpResponse("Already done.")
    try:
        result = paddle.process(event)
    except Exception as exc:  # Paddle retries on an error: keep the reason on the event for the admin
        log.exception("Paddle webhook %s failed", event.event_id)
        event.result = f"Failed: {exc}"[:300]
        event.save(update_fields=["result"])
        return HttpResponse("Failed; will be retried.", status=500)
    # Waiting for the customer: answer with an error so Paddle sends it again a little later.
    return HttpResponse(result, status=503 if event.processed is None else 200)
