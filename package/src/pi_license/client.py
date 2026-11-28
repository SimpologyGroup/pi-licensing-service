"""Server-to-server client the apps' backends use to talk to the licensing service (standard library only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .certificate import License, verify
from .keys import keys_from_jwks


class ServiceError(Exception):
    """The service answered with an error, e.g. `in_use` (the licence belongs to another account) or `not_usable`.
    `message` is plain wording that can be shown to the user."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class LicensingClient:
    """One per app. `api_key` identifies the app (its product) to the service; never send it to a browser."""

    def __init__(self, base_url: str, api_key: str, product: str, timeout: float = 10):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.product = product
        self.timeout = timeout
        self._keys: dict | None = None

    def _request(self, method: str, path: str, query: dict | None = None, body: dict | None = None) -> dict:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.api_key}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read() or b"{}")
            except ValueError:
                data = {}
            raise ServiceError(exc.code, data.get("error", "error"),
                               data.get("message", f"The licensing service answered {exc.code}.")) from exc

    @property
    def installation(self) -> str:
        """This app installation's id as certificates name it: the middle part of the API key."""
        parts = self.api_key.split("_")
        return parts[1] if len(parts) >= 3 else ""

    def public_keys(self, refresh: bool = False) -> dict:
        if self._keys is None or refresh:
            self._keys = keys_from_jwks(self._request("GET", "/.well-known/pi-license-keys"))
        return self._keys

    def verify(self, certificate: str) -> License:
        """Check a certificate's signature and dates for this app's product."""
        keys = self.public_keys()
        try:
            return verify(certificate, keys, product=self.product)
        except Exception:
            # A new signing key may have been added since the keys were cached.
            return verify(certificate, self.public_keys(refresh=True), product=self.product)

    def license_for_email(self, email: str, account_ref: str = "", org: str = "", org_id: str = "") -> dict:
        """The current licence for an email the account has confirmed with a code (free tier issued when there is
        none)."""
        query = {"email": email, "product": self.product}
        if account_ref:
            query["account"] = account_ref
        if org:
            query["org"] = org
        if org_id:
            query["org_id"] = org_id
        return self._request("GET", "/v1/accounts/licenses", query=query)

    def organisation(self, org_id: str, account_ref: str = "") -> dict:
        """{"org_id", "name"} of the organisation the account is linked to; ServiceError 404 `unknown_org` for
        any other."""
        return self._request("GET", f"/v1/organisations/{urllib.parse.quote(org_id.strip())}",
                             query={"account": account_ref} if account_ref else None)["organisation"]

    def licenses_for_org(self, org_id: str, account_ref: str) -> list:
        """The licences of every customer in the organisation for this product, to pick one (as licenses_for_email,
        each marked free to use, used by this account or by another). Only for the organisation the account is
        linked to; else ServiceError 403 `org_not_linked`."""
        return self._request("GET", "/v1/licenses", query={"org_id": org_id, "product": self.product,
                                                            "account": account_ref})["licenses"]

    def free_licence(self, email: str, account_ref: str, org_id: str = "") -> dict:
        """The free-tier licence of an app account (issued when it has none, tied to the account). For an account
        that has no licence yet: it falls back to the free tier without anyone picking a licence."""
        body = {"email": email, "product": self.product, "account": account_ref}
        if org_id:  # a new customer joins the account's organisation
            body["org_id"] = org_id
        return self._request("POST", "/v1/free-licenses", body=body)

    def licenses_for_email(self, email: str, account_ref: str) -> list:
        """The licences held under an email for this product, for an administrator to pick one. Each says
        whether it is free to use, already used by this account (`"account": "this"`) or by another (`"other"`).
        A free-tier licence is issued when the email has none this account could use. The email must be confirmed
        first (send_code, check_code); else ServiceError 403 `not_proven`."""
        return self._request("GET", "/v1/licenses", query={"email": email, "product": self.product,
                                                            "account": account_ref})["licenses"]

    def status(self, lid: str) -> dict:
        return self._request("GET", f"/v1/licenses/{lid}/status")

    def activate(self, lid: str, account_ref: str, org_id: str = "", certificate: str = "") -> dict:
        """Tie a licence to the account. With `org_id`, a licence of another organisation is refused
        (ServiceError 409 `other_org`). The account must have proved it may use the licence: its email confirmed
        with a code, its organisation linked, or `certificate` (the .pilic text); else ServiceError 403
        `not_proven`."""
        body = {"lid": lid, "account": account_ref}
        if org_id:
            body["org_id"] = org_id
        if certificate:
            body["certificate"] = certificate
        return self._request("POST", "/v1/activations", body=body)

    def send_code(self, email: str, account_ref: str, account_name: str = "", requested_by: str = "") -> dict:
        """Email a six-digit code to `email`, to prove it before its licences are listed or used. The email names
        the account and who asked. ServiceError 429 `too_many` when too many codes were asked for."""
        return self._request("POST", "/v1/email-codes", body={
            "email": email, "account": account_ref, "account_name": account_name, "requested_by": requested_by})

    def check_code(self, email: str, account_ref: str, code: str) -> dict:
        """{"confirmed": True, "until"}: the account may list and use the email's licences until then.
        ServiceError 400 `bad_code` (wrong or expired) or 429 `too_many` (too many wrong codes)."""
        return self._request("POST", "/v1/email-codes/check", body={
            "email": email, "account": account_ref, "code": code})

    def link_org(self, org_id: str, account_ref: str) -> dict:
        """Link the account to an organisation and return {"org_id", "name"}. Allowed once the account has
        confirmed the email of someone in it; else ServiceError 403 `not_proven` (404 `unknown_org`)."""
        return self._request("POST", "/v1/org-links", body={"org_id": org_id, "account": account_ref})["organisation"]
