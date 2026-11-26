"""Signing keys. The private key stays with the licensing service; apps get the public keys by kid."""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def generate() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def private_pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def load_private_pem(data: bytes) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Not an Ed25519 private key.")
    return key


def public_pem(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")


def public_jwk(key: Ed25519PrivateKey | Ed25519PublicKey, kid: str) -> dict:
    """The public key as a JWK (OKP / Ed25519), as served at /.well-known/pi-license-keys."""
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "kid": kid,
        "use": "sig",
        "alg": "EdDSA",
        "x": base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii"),
    }


def keys_from_jwks(jwks: dict) -> dict:
    """Turn a JWKS document into {kid: Ed25519PublicKey}."""
    result = {}
    for jwk in jwks.get("keys", []):
        if jwk.get("kty") == "OKP" and jwk.get("crv") == "Ed25519":
            raw = base64.urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4))
            result[jwk["kid"]] = Ed25519PublicKey.from_public_bytes(raw)
    return result
