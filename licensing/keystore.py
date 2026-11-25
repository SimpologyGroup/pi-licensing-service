"""Signing keys on disk: <SIGNING_KEYS_DIR>/<kid>.pem. Only this module touches private keys."""

from functools import lru_cache

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from pi_license import keys


def _dir():
    return settings.LICENSE_SIGNING_KEYS_DIR


@lru_cache(maxsize=None)
def _load(kid: str):
    path = _dir() / f"{kid}.pem"
    if not path.exists():
        raise ImproperlyConfigured(f"Signing key {kid!r} not found in {_dir()}.")
    return keys.load_private_pem(path.read_bytes())


def current():
    """(kid, private key) used for new certificates."""
    kid = settings.LICENSE_SIGNING_KID
    if not kid:
        raise ImproperlyConfigured("SIGNING_KID is not set. Run manage.py generate_signing_key.")
    return kid, _load(kid)


def all_kids():
    return sorted(p.stem for p in _dir().glob("*.pem")) if _dir().exists() else []


def jwks():
    """Public keys of every key on disk, so certificates signed with an older key still verify."""
    return {"keys": [keys.public_jwk(_load(kid), kid) for kid in all_kids()]}


def clear_cache():
    _load.cache_clear()
