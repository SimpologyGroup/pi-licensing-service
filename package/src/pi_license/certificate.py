"""Signed licence certificates (.pilic files).

A certificate is a compact JWS (header.payload.signature, base64url) signed with Ed25519 ("EdDSA"),
wrapped in a readable envelope. Only the licensing service holds the private key; the apps hold the
public keys by key id ("kid"), so a certificate cannot be edited without breaking the signature.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ISSUER = "pi-licensing"
# Optional claims added later ("account", "installation") are left out when empty and ignored by older packages,
# so they did not need a new version. A change older packages would misread does.
SCHEMA_VERSION = 1
TYP = "pi-license+jws"
BEGIN = "-----BEGIN PI LICENSE-----"
END = "-----END PI LICENSE-----"


class LicenseError(Exception):
    """The certificate cannot be accepted. `code` says why, for logs and tests."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@dataclass(frozen=True)
class License:
    """What a verified certificate says. Limits map counter names to a maximum; None means unlimited."""

    lid: str
    product: str
    plan: str
    email: str
    org: str
    limits: dict
    features: list
    subscription: dict
    issued_at: datetime
    valid_from: datetime
    expires_at: datetime
    grace_days: int
    check_after: datetime
    raw: dict = field(repr=False, default_factory=dict)
    # The app account the licence is tied to (its schema name) and the app installation (the first part of that
    # app's API key, "pil_<installation>_..."); empty while the licence is not tied to an account yet.
    account: str = ""
    installation: str = ""
    # The organisation's Org ID (ORG-00001 ...), the same in every PI app; `org` is its name. Empty in older
    # certificates and for customers without an organisation.
    org_id: str = ""

    def belongs_elsewhere(self, account: str, installation: str = "") -> bool:
        """True if the certificate names another account (or another installation) than the one given."""
        if self.account and self.account != account:
            return True
        return bool(self.installation and installation and self.installation != installation)

    def limit(self, name: str):
        """The cap for one counter, or None for unlimited. A counter the plan does not name is 0."""
        if name not in self.limits:
            return 0
        return self.limits[name]

    def allows(self, name: str, current_count: int, adding: int = 1) -> bool:
        """True if `adding` more of `name` stays within the cap."""
        cap = self.limit(name)
        return cap is None or current_count + adding <= cap


def sign(payload: dict, private_key: Ed25519PrivateKey, kid: str) -> str:
    """Sign a payload and return the compact JWS token."""
    header = {"alg": "EdDSA", "kid": kid, "typ": TYP}
    signing_input = (
        _b64e(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
        + "."
        + _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    )
    signature = private_key.sign(signing_input.encode("ascii"))
    return signing_input + "." + _b64e(signature)


def wrap(token: str, width: int = 64) -> str:
    """Put a token in the readable .pilic envelope."""
    lines = [token[i : i + width] for i in range(0, len(token), width)]
    return "\n".join([BEGIN, *lines, END]) + "\n"


def unwrap(text: str) -> str:
    """Get the token back from an envelope (or accept a bare token, e.g. pasted text)."""
    text = text.strip()
    if BEGIN in text:
        start = text.index(BEGIN) + len(BEGIN)
        end = text.index(END) if END in text else len(text)
        text = text[start:end]
    return "".join(text.split())


def build_payload(
    *,
    lid: str,
    customer_id: str,
    email: str,
    org: str,
    product: str,
    plan: str,
    limits: dict,
    features: list | None = None,
    subscription: dict | None = None,
    valid_from: datetime,
    expires_at: datetime,
    grace_days: int = 7,
    check_hours: int = 6,
    issued_at: datetime | None = None,
    account: str = "",
    installation: str = "",
    org_id: str = "",
) -> dict:
    issued_at = issued_at or datetime.now(timezone.utc)
    payload = {
        "ver": SCHEMA_VERSION,
        "iss": ISSUER,
        "lid": lid,
        "sub": customer_id,
        "email": email,
        "org": org,
        "product": product,
        "plan": plan,
        "features": list(features or []),
        "limits": dict(limits),
        "subscription": dict(subscription or {}),
        "iat": _iso(issued_at),
        "nbf": _iso(valid_from),
        "exp": _iso(expires_at),
        "grace_days": grace_days,
        "check_after": _iso(issued_at + timedelta(hours=check_hours)),
    }
    if org_id:  # left out when empty, like account/installation: older packages ignore it
        payload["org_id"] = org_id
    if account:  # only once the licence is tied to an app account
        payload["account"] = account
        payload["installation"] = installation
    return payload


def load_public_key(raw: bytes | str) -> Ed25519PublicKey:
    """Load a public key from PEM text or from the 32 raw bytes (base64url text also accepted)."""
    from cryptography.hazmat.primitives import serialization

    if isinstance(raw, str):
        if "BEGIN PUBLIC KEY" in raw:
            raw = raw.encode()
        else:
            return Ed25519PublicKey.from_public_bytes(_b64d(raw))
    if b"BEGIN PUBLIC KEY" in raw:
        key = serialization.load_pem_public_key(raw)
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("Not an Ed25519 public key.")
        return key
    return Ed25519PublicKey.from_public_bytes(raw)


def verify(
    text: str,
    public_keys: dict,
    *,
    product: str | None = None,
    now: datetime | None = None,
    allow_grace: bool = True,
) -> License:
    """Check a certificate and return what it grants.

    `public_keys` maps kid to an Ed25519PublicKey (or PEM / base64url text). `product`, when given, must match.
    Raises LicenseError on a bad signature, wrong issuer or version, another product, or dates outside
    nbf .. exp (+ grace_days when allow_grace).
    """
    token = unwrap(text)
    parts = token.split(".")
    if len(parts) != 3:
        raise LicenseError("malformed", "This is not a licence certificate.")
    try:
        header = json.loads(_b64d(parts[0]))
        payload = json.loads(_b64d(parts[1]))
        signature = _b64d(parts[2])
    except (ValueError, json.JSONDecodeError) as exc:
        raise LicenseError("malformed", "This is not a licence certificate.") from exc

    if header.get("alg") != "EdDSA" or header.get("typ") != TYP:
        raise LicenseError("malformed", "This is not a licence certificate.")
    key = public_keys.get(header.get("kid"))
    if key is None:
        raise LicenseError("unknown_key", "This certificate was signed with a key this app does not know.")
    if not isinstance(key, Ed25519PublicKey):
        key = load_public_key(key)
    try:
        key.verify(signature, (parts[0] + "." + parts[1]).encode("ascii"))
    except InvalidSignature as exc:
        raise LicenseError("bad_signature", "This certificate has been changed or is not genuine.") from exc

    if payload.get("iss") != ISSUER:
        raise LicenseError("wrong_issuer", "This certificate was not issued by the PI licensing service.")
    if payload.get("ver") != SCHEMA_VERSION:
        raise LicenseError("wrong_version", "This certificate's format is not supported.")
    if product is not None and payload.get("product") != product:
        raise LicenseError("wrong_product", "This certificate is for another product.")

    now = now or datetime.now(timezone.utc)
    valid_from = _parse_iso(payload["nbf"])
    expires_at = _parse_iso(payload["exp"])
    grace = timedelta(days=payload.get("grace_days", 0) if allow_grace else 0)
    if now < valid_from:
        raise LicenseError("not_yet_valid", "This certificate is not valid yet.")
    if now > expires_at + grace:
        raise LicenseError("expired", "This certificate has expired.")

    return License(
        lid=payload["lid"],
        product=payload["product"],
        plan=payload["plan"],
        email=payload.get("email", ""),
        org=payload.get("org", ""),
        limits=payload.get("limits", {}),
        features=payload.get("features", []),
        subscription=payload.get("subscription", {}),
        issued_at=_parse_iso(payload["iat"]),
        valid_from=valid_from,
        expires_at=expires_at,
        grace_days=payload.get("grace_days", 0),
        check_after=_parse_iso(payload["check_after"]),
        raw=payload,
        account=payload.get("account", ""),
        installation=payload.get("installation", ""),
        org_id=payload.get("org_id", ""),
    )
