"""JSON API. Every endpoint except the public keys needs `Authorization: Bearer <app key>`.

App keys belong to one product (an app's backend); the website's key has no product and may act for any.
These calls are server to server only: keys are never sent to a browser.
"""

import json
from functools import wraps

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import keystore, services
from .models import AppClient, License


def error(status, code, message):
    return JsonResponse({"error": code, "message": message}, status=status)


def api_key_required(view):
    @csrf_exempt
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        header = request.headers.get("Authorization", "")
        key = header[7:].strip() if header.startswith("Bearer ") else ""
        client = AppClient.from_key(key) if key else None
        if client is None:
            return error(401, "unauthorised", "A valid API key is needed.")
        request.app_client = client
        return view(request, *args, **kwargs)

    return wrapper


def _product_for(request, code):
    """The product asked for, refused when an app's key asks about another app's product."""
    client = request.app_client
    if client.product_id and code and code != client.product.code:
        raise PermissionError
    return services.get_product(code or (client.product.code if client.product_id else ""))


def _license_for_caller(request, lid):
    license_ = License.objects.select_related("tier", "product", "customer").filter(lid=lid).first()
    if license_ is None:
        return None
    client = request.app_client
    if client.product_id and license_.product_id != client.product_id:
        return None
    return license_


def _json_body(request):
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return None


def _valid_email(email):
    try:
        validate_email(email)
        return True
    except ValidationError:
        return False


@require_GET
def public_keys(request):
    return JsonResponse(keystore.jwks())


@require_GET
@api_key_required
def account_license(request):
    """Automatic linking: the licence to use for a verified email (free tier issued when there is none).
    With `account` (the app account's schema name) only licences free to use or already tied to that account count,
    and the one returned is tied to it."""
    email = services.normalise_email(request.GET.get("email", ""))
    if not _valid_email(email):
        return error(400, "bad_email", "A valid email is needed.")
    try:
        product = _product_for(request, request.GET.get("product", ""))
    except PermissionError:
        return error(403, "wrong_product", "This key may not ask about that product.")
    except services.NotFound as exc:
        return error(404, "unknown_product", str(exc))
    customer = services.get_or_create_customer(email, request.GET.get("name", ""), request.GET.get("org", ""),
                                               request.GET.get("org_id", ""))
    account = request.GET.get("account", "").strip()
    license_ = services.current_license(customer, product, app_client=request.app_client, account_ref=account)
    if account:
        services.record_activation(license_, request.app_client, account)
    return JsonResponse({"license": services.describe(license_)})


@require_GET
@api_key_required
def licenses_for_email(request):
    """The licences held under an email (or by every customer of an organisation, with `org_id`) for the product,
    for an app administrator to pick the one for their account
    (`account`, its schema name). Each says whether it is free to use, used by this account or by another one.
    No certificates: the chosen one is tied to the account with POST /v1/activations, which returns it."""
    org_id = request.GET.get("org_id", "")
    email = services.normalise_email(request.GET.get("email", ""))
    if not org_id and not _valid_email(email):
        return error(400, "bad_email", "A valid email (or an Org ID) is needed.")
    account = request.GET.get("account", "").strip()
    if not account:
        return error(400, "bad_account", "The app account is needed.")
    try:
        product = _product_for(request, request.GET.get("product", ""))
    except PermissionError:
        return error(403, "wrong_product", "This key may not ask about that product.")
    except services.NotFound as exc:
        return error(404, "unknown_product", str(exc))
    if org_id:
        organisation = services.find_organisation(org_id)
        if organisation is None:
            return error(404, "unknown_org", "There is no organisation with that Org ID.")
        licenses = services.licenses_for_org(organisation, product)
    else:
        customer = services.get_or_create_customer(email)
        licenses = services.licenses_for_email(customer, product, request.app_client, account)
    return JsonResponse({"licenses": [services.summary(x, request.app_client, account) for x in licenses]})


@require_GET
@api_key_required
def organisation(request, org_id):
    """An organisation by its Org ID, so an app can show its name before linking an account to it."""
    found = services.find_organisation(org_id)
    if found is None:
        return error(404, "unknown_org", "There is no organisation with that Org ID.")
    return JsonResponse({"organisation": {"org_id": found.org_id, "name": found.name}})


@require_POST
@api_key_required
def free_license(request):
    """Website sign-up: issue (or return) the free-tier licence for an email and product. With `account` (an app
    account's schema name): the free licence of that account, tied to it (never a paid one)."""
    body = _json_body(request)
    if body is None:
        return error(400, "bad_json", "The request body is not valid JSON.")
    email = services.normalise_email(body.get("email", ""))
    if not _valid_email(email):
        return error(400, "bad_email", "A valid email is needed.")
    try:
        product = _product_for(request, body.get("product", ""))
    except PermissionError:
        return error(403, "wrong_product", "This key may not ask about that product.")
    except services.NotFound as exc:
        return error(404, "unknown_product", str(exc))
    customer = services.get_or_create_customer(email, body.get("name", ""), body.get("org", ""),
                                               str(body.get("org_id", "")))
    account = str(body.get("account", "")).strip()
    with transaction.atomic():
        license_ = services.ensure_free_license(customer, product, app_client=request.app_client, account_ref=account)
        if account:
            # An app account without a licence falls back to the free tier: its own free licence, tied to it.
            services.record_activation(license_, request.app_client, account)
    return JsonResponse({"license": services.describe(license_)}, status=201)


@require_GET
@api_key_required
def license_status(request, lid):
    """Status of one licence. When it is no longer usable, `current` is the licence now in use (free tier)."""
    license_ = _license_for_caller(request, lid)
    if license_ is None:
        return error(404, "not_found", "No such licence.")
    usable = services.is_usable(license_)
    current = license_
    if not usable:
        # The account it belonged to moves on to the customer's next licence for it (free tier at least).
        act = services.binding(license_)
        if act is not None and act.app_client_id == request.app_client.pk:
            current = services.current_license(license_.customer, license_.product,
                                               app_client=request.app_client, account_ref=act.account_ref)
            services.record_activation(current, request.app_client, act.account_ref)
        else:
            current = services.current_license(license_.customer, license_.product)
    return JsonResponse(
        {
            "lid": license_.lid,
            "status": license_.status,
            "usable": usable,
            "current": services.describe(current),
        }
    )


@require_GET
@api_key_required
def license_certificate(request, lid):
    """Download the current certificate as a .pilic file."""
    license_ = _license_for_caller(request, lid)
    if license_ is None:
        return error(404, "not_found", "No such licence.")
    services.refresh(license_)
    text = services.describe(license_)["certificate"]
    response = HttpResponse(text, content_type="application/x-pi-license; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{license_.product.code}-{license_.lid}.pilic"'
    return response


@require_POST
@api_key_required
def activation(request):
    """Tie a licence to an app account (its schema name), e.g. after the customer uploads a certificate. With
    `org_id` (the organisation the app account is linked to) a licence of another organisation is refused."""
    body = _json_body(request)
    if body is None:
        return error(400, "bad_json", "The request body is not valid JSON.")
    license_ = _license_for_caller(request, body.get("lid", ""))
    if license_ is None:
        return error(404, "not_found", "No such licence.")
    account = (body.get("account") or "").strip()
    if not account:
        return error(400, "bad_account", "The app account is needed.")
    if not services.is_usable(license_):
        return error(409, "not_usable", f"This licence is {license_.get_status_display().lower()}.")
    org = services.find_organisation(str(body.get("org_id", "")))
    held_by = license_.customer.organisation
    if body.get("org_id") and held_by is not None and held_by != org:
        # an app account linked to an organisation only takes that organisation's licences
        return error(409, "other_org", f"This licence belongs to another organisation ({held_by.org_id}).")
    try:
        services.record_activation(license_, request.app_client, account)
    except services.InUse as exc:
        return error(409, "in_use", str(exc))
    return JsonResponse({"license": services.describe(license_)}, status=201)
