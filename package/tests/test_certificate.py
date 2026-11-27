import unittest
from datetime import datetime, timedelta, timezone

from pi_license import LicenseError, build_payload, sign, unwrap, verify, wrap
from pi_license import keys

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def make(product="pi-safety", limits=None, expires_days=365, grace_days=7, key=None, kid="k1", **extra):
    key = key or KEY
    payload = build_payload(
        lid="lic_1",
        customer_id="cus_1",
        email="jane@acme.co",
        org="Acme",
        product=product,
        plan="safety-t1",
        limits=limits if limits is not None else {"org_units": 5, "resources": 10},
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=expires_days),
        grace_days=grace_days,
        issued_at=NOW,
        account=extra.pop("account", ""),
        installation=extra.pop("installation", ""),
        org_id=extra.pop("org_id", ""),
    )
    payload.update(extra)
    return wrap(sign(payload, key, kid))


KEY = keys.generate()
PUBLIC = {"k1": KEY.public_key()}


class CertificateTests(unittest.TestCase):
    def test_round_trip(self):
        lic = verify(make(), PUBLIC, product="pi-safety", now=NOW)
        self.assertEqual(lic.plan, "safety-t1")
        self.assertEqual(lic.limits, {"org_units": 5, "resources": 10})
        self.assertEqual(lic.email, "jane@acme.co")

    def test_org_id_claim_only_when_set(self):
        self.assertEqual(verify(make(), PUBLIC, now=NOW).org_id, "")
        self.assertNotIn("org_id", verify(make(), PUBLIC, now=NOW).raw)
        lic = verify(make(org_id="ORG-00007"), PUBLIC, now=NOW)
        self.assertEqual((lic.org_id, lic.org), ("ORG-00007", "Acme"))

    def test_envelope_and_bare_token_both_accepted(self):
        text = make()
        self.assertTrue(text.startswith("-----BEGIN PI LICENSE-----"))
        verify(unwrap(text), PUBLIC, now=NOW)

    def test_tampered_payload_rejected(self):
        token = unwrap(make())
        head, body, sig = token.split(".")
        other = unwrap(make(limits={"org_units": 500, "resources": 750}))
        forged = ".".join([head, other.split(".")[1], sig])
        with self.assertRaises(LicenseError) as ctx:
            verify(forged, PUBLIC, now=NOW)
        self.assertEqual(ctx.exception.code, "bad_signature")

    def test_other_key_rejected(self):
        with self.assertRaises(LicenseError) as ctx:
            verify(make(key=keys.generate()), PUBLIC, now=NOW)
        self.assertEqual(ctx.exception.code, "bad_signature")

    def test_unknown_kid_rejected(self):
        with self.assertRaises(LicenseError) as ctx:
            verify(make(kid="k9"), PUBLIC, now=NOW)
        self.assertEqual(ctx.exception.code, "unknown_key")

    def test_wrong_product_rejected(self):
        with self.assertRaises(LicenseError) as ctx:
            verify(make(product="pi-cashflow"), PUBLIC, product="pi-safety", now=NOW)
        self.assertEqual(ctx.exception.code, "wrong_product")

    def test_expiry_and_grace(self):
        text = make(expires_days=1, grace_days=7)
        verify(text, PUBLIC, now=NOW + timedelta(days=5))  # inside grace
        with self.assertRaises(LicenseError):
            verify(text, PUBLIC, now=NOW + timedelta(days=5), allow_grace=False)
        with self.assertRaises(LicenseError) as ctx:
            verify(text, PUBLIC, now=NOW + timedelta(days=9))
        self.assertEqual(ctx.exception.code, "expired")

    def test_wrong_issuer_rejected(self):
        with self.assertRaises(LicenseError) as ctx:
            verify(make(iss="someone-else"), PUBLIC, now=NOW)
        self.assertEqual(ctx.exception.code, "wrong_issuer")

    def test_garbage_rejected(self):
        for text in ["", "hello", "a.b.c", "-----BEGIN PI LICENSE-----\nxyz\n-----END PI LICENSE-----"]:
            with self.assertRaises(LicenseError):
                verify(text, PUBLIC, now=NOW)

    def test_limits(self):
        lic = verify(make(limits={"users": 3, "cashflow_lines": None}, product="pi-cashflow"), PUBLIC, now=NOW)
        self.assertTrue(lic.allows("users", 2))
        self.assertFalse(lic.allows("users", 3))
        self.assertTrue(lic.allows("cashflow_lines", 10_000))  # None = unlimited
        self.assertFalse(lic.allows("sites", 0))  # a counter the plan does not name is 0

    def test_jwks_round_trip(self):
        jwk = keys.public_jwk(KEY, "k1")
        loaded = keys.keys_from_jwks({"keys": [jwk]})
        verify(make(), loaded, now=NOW)

    def test_pem_round_trip(self):
        private = keys.load_private_pem(keys.private_pem(KEY))
        verify(make(key=private), {"k1": keys.public_pem(KEY)}, now=NOW)


if __name__ == "__main__":
    unittest.main()


class AccountClaimTests(unittest.TestCase):
    def test_unbound_certificate_has_no_account(self):
        lic = verify(make(), PUBLIC, product="pi-safety", now=NOW)
        self.assertEqual((lic.account, lic.installation), ("", ""))
        self.assertNotIn("account", lic.raw)
        self.assertFalse(lic.belongs_elsewhere("anything"))

    def test_bound_certificate_names_account_and_installation(self):
        lic = verify(make(account="acme", installation="ab12cd34"), PUBLIC, product="pi-safety", now=NOW)
        self.assertEqual((lic.account, lic.installation), ("acme", "ab12cd34"))
        self.assertTrue(lic.belongs_elsewhere("other"))
        self.assertTrue(lic.belongs_elsewhere("acme", "ffff0000"))
        self.assertFalse(lic.belongs_elsewhere("acme", "ab12cd34"))


class ServiceErrorTests(unittest.TestCase):
    def test_error_answer_becomes_service_error(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        from pi_license import LicensingClient, ServiceError

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.dumps({"error": "in_use", "message": "Used by another account."}).encode()
                self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = LicensingClient(f"http://127.0.0.1:{server.server_port}", "pil_ab12cd34_secret", "pi-safety")
            with self.assertRaises(ServiceError) as ctx:
                client.activate("lic_1", "acme")
            self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "in_use"))
            self.assertEqual(str(ctx.exception), "Used by another account.")
            self.assertEqual(client.installation, "ab12cd34")
        finally:
            server.shutdown()
