import json
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from pi_license import LicenseError, keys, verify

from . import keystore, services
from .models import Activation, AppClient, Certificate, Customer, License, Product, Tier

KEY_DIR = Path(tempfile.mkdtemp(prefix="pi-lic-keys-"))
(KEY_DIR / "test-1.pem").write_bytes(keys.private_pem(keys.generate()))


@override_settings(LICENSE_SIGNING_KEYS_DIR=KEY_DIR, LICENSE_SIGNING_KID="test-1", LICENSE_GRACE_DAYS=7)
class LicensingTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        keystore.clear_cache()

    def setUp(self):
        keystore.clear_cache()
        self.safety = Product.objects.get(code="pi-safety")
        self.cashflow = Product.objects.get(code="pi-cashflow")
        _, self.safety_key = AppClient.create_with_key("Safety app", self.safety)
        _, self.cashflow_key = AppClient.create_with_key("Cash Flow app", self.cashflow)
        _, self.website_key = AppClient.create_with_key("Website")

    def public_keys(self):
        return keys.keys_from_jwks(self.client.get(reverse("public_keys")).json())

    def get(self, name, key, **kwargs):
        url = reverse(name, kwargs=kwargs.pop("url_kwargs", None))
        return self.client.get(url, kwargs, HTTP_AUTHORIZATION=f"Bearer {key}")

    def post(self, name, key, body):
        return self.client.post(
            reverse(name), json.dumps(body), content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {key}"
        )

    def paid_license(self, email, tier_code, days_left=200, status=License.ACTIVE):
        tier = Tier.objects.get(code=tier_code)
        now = timezone.now()
        return License.objects.create(
            customer=services.get_or_create_customer(email),
            product=tier.product,
            tier=tier,
            status=status,
            period_start=now - timedelta(days=365 - days_left),
            period_end=now + timedelta(days=days_left),
            provider="paddle",
            provider_subscription_id="sub_1",
        )


class CatalogueTests(LicensingTestCase):
    def test_agreed_tiers_loaded(self):
        safety = {t.code: (t.limits, str(t.price_usd), t.is_free) for t in self.safety.tiers.all()}
        self.assertEqual(safety["safety-t1"], ({"org_units": 5, "resources": 10}, "0.00", True))
        self.assertEqual(safety["safety-t2"], ({"org_units": 15, "resources": 25}, "198.00", False))
        self.assertEqual(safety["safety-t3"], ({"org_units": 50, "resources": 75}, "396.00", False))
        self.assertEqual(safety["safety-t4"], ({"org_units": 500, "resources": 750}, "792.00", False))
        cashflow = {t.code: (t.limits, str(t.price_usd)) for t in self.cashflow.tiers.all()}
        self.assertEqual(cashflow["cashflow-t1"], ({"users": 1, "cashflow_lines": 50}, "0.00"))
        self.assertEqual(cashflow["cashflow-t2"], ({"users": 3, "cashflow_lines": 200}, "110.00"))
        self.assertEqual(cashflow["cashflow-t3"], ({"users": 5, "cashflow_lines": 400}, "220.00"))
        self.assertEqual(cashflow["cashflow-t4"], ({"users": 25, "cashflow_lines": 2000}, "440.00"))
        self.assertTrue(all(t.interval == "year" for t in Tier.objects.all()))

    def test_new_product_needs_no_code(self):
        product = Product.objects.create(code="pi-new", name="New", limit_names=["widgets"])
        Tier.objects.create(product=product, code="new-t1", name="Tier 1", limits={"widgets": 3}, is_free=True)
        r = self.get("account_license", self.website_key, email="a@b.co", product="pi-new")
        self.assertEqual(r.status_code, 200)
        lic = verify(r.json()["license"]["certificate"], self.public_keys(), product="pi-new")
        self.assertEqual(lic.limits, {"widgets": 3})


class FreeTierTests(LicensingTestCase):
    def test_website_signup_issues_free_certificate(self):
        r = self.post("free_license", self.website_key, {"email": "Jane@Acme.co ", "product": "pi-safety", "org": "Acme"})
        self.assertEqual(r.status_code, 201)
        data = r.json()["license"]
        self.assertEqual(data["plan"], "safety-t1")
        self.assertTrue(data["free"])
        lic = verify(data["certificate"], self.public_keys(), product="pi-safety")
        self.assertEqual(lic.email, "jane@acme.co")
        self.assertEqual(lic.org, "Acme")
        self.assertEqual(lic.limits, {"org_units": 5, "resources": 10})
        self.assertEqual(lic.subscription["provider"], "free")

    def test_signup_twice_gives_the_same_licence(self):
        first = self.post("free_license", self.website_key, {"email": "j@a.co", "product": "pi-safety"}).json()
        second = self.post("free_license", self.website_key, {"email": "J@A.co", "product": "pi-safety"}).json()
        self.assertEqual(first["license"]["lid"], second["license"]["lid"])
        self.assertEqual(Certificate.objects.count(), 1)

    def test_free_licence_renews_itself(self):
        customer = services.get_or_create_customer("j@a.co")
        license_ = services.ensure_free_license(customer, self.safety)
        later = license_.period_end + timedelta(days=3)
        renewed = services.current_license(customer, self.safety, now=later)
        self.assertEqual(renewed.pk, license_.pk)
        self.assertGreater(renewed.period_end, later)
        self.assertEqual(renewed.status, License.ACTIVE)

    def test_each_product_has_its_own_free_licence(self):
        customer = services.get_or_create_customer("j@a.co")
        a = services.current_license(customer, self.safety)
        b = services.current_license(customer, self.cashflow)
        self.assertNotEqual(a.pk, b.pk)
        self.assertEqual(b.tier.code, "cashflow-t1")


class AutoLinkTests(LicensingTestCase):
    def test_lookup_by_email_issues_free_tier_and_records_account(self):
        r = self.get("account_license", self.safety_key, email="new@co.za", account="acme")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["license"]["plan"], "safety-t1")
        self.assertEqual(Activation.objects.get().account_ref, "acme")

    def test_paid_licence_wins_over_free(self):
        self.paid_license("pay@co.za", "safety-t3")
        r = self.get("account_license", self.safety_key, email="PAY@co.za")
        data = r.json()["license"]
        self.assertEqual(data["plan"], "safety-t3")
        lic = verify(data["certificate"], self.public_keys(), product="pi-safety")
        self.assertEqual(lic.limits, {"org_units": 50, "resources": 75})
        self.assertEqual(lic.grace_days, 7)

    def test_lapsed_paid_licence_drops_to_tier_1(self):
        paid = self.paid_license("late@co.za", "safety-t2", days_left=-10)  # past end + 7 grace days
        r = self.get("account_license", self.safety_key, email="late@co.za")
        self.assertEqual(r.json()["license"]["plan"], "safety-t1")
        paid.refresh_from_db()
        self.assertEqual(paid.status, License.EXPIRED)

    def test_paid_licence_still_counts_in_grace(self):
        self.paid_license("grace@co.za", "safety-t2", days_left=-3)
        r = self.get("account_license", self.safety_key, email="grace@co.za")
        self.assertEqual(r.json()["license"]["plan"], "safety-t2")

    def test_cancelled_licence_runs_to_period_end(self):
        self.paid_license("bye@co.za", "cashflow-t2", status=License.CANCELED)
        r = self.get("account_license", self.cashflow_key, email="bye@co.za")
        self.assertEqual(r.json()["license"]["plan"], "cashflow-t2")

    def test_revoked_licence_is_not_used(self):
        self.paid_license("bad@co.za", "safety-t4", status=License.REVOKED)
        r = self.get("account_license", self.safety_key, email="bad@co.za")
        self.assertEqual(r.json()["license"]["plan"], "safety-t1")


class StatusAndAccessTests(LicensingTestCase):
    def test_status_of_lapsed_licence_points_to_current(self):
        paid = self.paid_license("late@co.za", "safety-t2", days_left=-10)
        r = self.get("license_status", self.safety_key, url_kwargs={"lid": paid.lid})
        data = r.json()
        self.assertFalse(data["usable"])
        self.assertEqual(data["status"], "expired")
        self.assertEqual(data["current"]["plan"], "safety-t1")

    def test_certificate_download(self):
        paid = self.paid_license("p@co.za", "safety-t2")
        r = self.get("license_certificate", self.safety_key, url_kwargs={"lid": paid.lid})
        self.assertEqual(r.status_code, 200)
        self.assertIn(".pilic", r["Content-Disposition"])
        self.assertEqual(verify(r.content.decode(), self.public_keys()).plan, "safety-t2")

    def test_certificate_reissued_when_tier_changes(self):
        paid = self.paid_license("p@co.za", "safety-t2")
        first = services.current_certificate(paid)
        paid.tier = Tier.objects.get(code="safety-t3")
        paid.save()
        second = services.current_certificate(paid)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(second.plan, "safety-t3")

    def test_activation_after_upload(self):
        paid = self.paid_license("p@co.za", "cashflow-t3")
        r = self.post("activation", self.cashflow_key, {"lid": paid.lid, "account": "simpology"})
        self.assertEqual(r.status_code, 201)
        self.assertTrue(Activation.objects.filter(license=paid, account_ref="simpology").exists())

    def test_activation_of_expired_licence_refused(self):
        paid = self.paid_license("p@co.za", "cashflow-t3", days_left=-30)
        r = self.post("activation", self.cashflow_key, {"lid": paid.lid, "account": "simpology"})
        self.assertEqual(r.status_code, 409)

    def test_no_key_refused(self):
        r = self.client.get(reverse("account_license"), {"email": "a@b.co", "product": "pi-safety"})
        self.assertEqual(r.status_code, 401)
        r = self.client.get(
            reverse("account_license"), {"email": "a@b.co"}, HTTP_AUTHORIZATION="Bearer pil_dead_beef"
        )
        self.assertEqual(r.status_code, 401)

    def test_switched_off_key_refused(self):
        AppClient.objects.filter(product=self.safety).update(active=False)
        r = self.get("account_license", self.safety_key, email="a@b.co")
        self.assertEqual(r.status_code, 401)

    def test_app_key_cannot_see_other_product(self):
        r = self.get("account_license", self.safety_key, email="a@b.co", product="pi-cashflow")
        self.assertEqual(r.status_code, 403)
        paid = self.paid_license("p@co.za", "cashflow-t2")
        r = self.get("license_status", self.safety_key, url_kwargs={"lid": paid.lid})
        self.assertEqual(r.status_code, 404)

    def test_bad_email_refused(self):
        r = self.get("account_license", self.safety_key, email="not-an-email")
        self.assertEqual(r.status_code, 400)

    def test_only_key_hash_stored(self):
        client = AppClient.objects.get(name="Website")
        self.assertNotIn(self.website_key, client.key_hash)
        self.assertEqual(client.key_hash, AppClient.hash_key(self.website_key))


class KeyRotationTests(LicensingTestCase):
    def test_old_certificates_verify_after_rotation(self):
        old = services.current_certificate(services.current_license(services.get_or_create_customer("a@b.co"), self.safety))
        (KEY_DIR / "test-2.pem").write_bytes(keys.private_pem(keys.generate()))
        try:
            keystore.clear_cache()
            with self.settings(LICENSE_SIGNING_KID="test-2"):
                public = self.public_keys()
                self.assertEqual(set(public), {"test-1", "test-2"})
                verify(old.token, public)
                new = services.current_certificate(old.license)
                self.assertEqual(new.kid, "test-2")
        finally:
            (KEY_DIR / "test-2.pem").unlink()
            keystore.clear_cache()

    def test_tampered_certificate_rejected(self):
        license_ = services.current_license(services.get_or_create_customer("a@b.co"), self.safety)
        token = services.current_certificate(license_).token
        head, body, sig = token.split(".")
        with self.assertRaises(LicenseError):
            verify(head + "." + body[:-2] + "AA." + sig, self.public_keys())


def tearDownModule():
    shutil.rmtree(KEY_DIR, ignore_errors=True)


class TestScreenTests(LicensingTestCase):
    """Issuing licences by hand in the admin, with no payment."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth.models import User

        self.staff = User.objects.create_superuser("admin", "admin@x.co", "pw-long-enough")
        self.client.force_login(self.staff)

    def test_issue_paid_licence_without_payment(self):
        tier = Tier.objects.get(code="safety-t3")
        r = self.client.post(reverse("admin:licensing_license_add"),
                             {"email": "Test@Acme.co", "org": "Acme", "tier": tier.pk, "status": "active", "months": 12})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        license_ = License.objects.get(customer__email="test@acme.co")
        self.assertEqual(license_.tier, tier)
        self.assertEqual(license_.provider, "manual")
        r = self.get("account_license", self.safety_key, email="test@acme.co")
        self.assertEqual(r.json()["license"]["plan"], "safety-t3")

    def test_add_page_shows(self):
        self.assertEqual(self.client.get(reverse("admin:licensing_license_add")).status_code, 200)
        lic = self.paid_license("p@co.za", "safety-t2")
        self.assertEqual(self.client.get(reverse("admin:licensing_license_change", args=[lic.pk])).status_code, 200)

    def test_expire_action_drops_to_free(self):
        lic = self.paid_license("p@co.za", "safety-t2")
        self.client.post(reverse("admin:licensing_license_changelist"),
                         {"action": "expire_now", "_selected_action": [lic.pk]})
        r = self.get("account_license", self.safety_key, email="p@co.za")
        self.assertEqual(r.json()["license"]["plan"], "safety-t1")

    def test_revoke_and_reactivate(self):
        lic = self.paid_license("p@co.za", "cashflow-t4")
        url = reverse("admin:licensing_license_changelist")
        self.client.post(url, {"action": "revoke", "_selected_action": [lic.pk]})
        self.assertEqual(self.get("account_license", self.cashflow_key, email="p@co.za").json()["license"]["plan"],
                         "cashflow-t1")
        self.client.post(url, {"action": "reactivate", "_selected_action": [lic.pk]})
        self.assertEqual(self.get("account_license", self.cashflow_key, email="p@co.za").json()["license"]["plan"],
                         "cashflow-t4")
