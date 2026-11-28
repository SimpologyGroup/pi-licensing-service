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
from .models import (Activation, AppClient, Certificate, Customer, EmailProof, License, Organisation, OrgLink,
                     Product, Tier)

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

    # The tests of licence rules act as an account that has confirmed its emails (proof.py); ProofTests turn it off.
    prove = True

    def _prove(self, key, account, email="", lid=""):
        if not self.prove:
            return
        client = AppClient.from_key(key)
        if client is None or client.product_id is None:
            return
        if lid and not email:
            found = License.objects.filter(lid=lid).select_related("customer").first()
            email = found.customer.email if found else ""
        if email:
            EmailProof.objects.update_or_create(app_client=client, account_ref=account or "",
                                                email=services.normalise_email(email),
                                                defaults={"verified_at": timezone.now()})

    def _link(self, key, account, org_id):
        client = AppClient.from_key(key)
        org = services.find_organisation(org_id)
        if self.prove and client and org:
            OrgLink.objects.get_or_create(app_client=client, account_ref=account, organisation=org)

    def get(self, name, key, **kwargs):
        url = reverse(name, kwargs=kwargs.pop("url_kwargs", None))
        self._prove(key, kwargs.get("account", ""), kwargs.get("email", ""))
        if kwargs.get("org_id"):
            self._link(key, kwargs.get("account", ""), kwargs["org_id"])
        return self.client.get(url, kwargs, HTTP_AUTHORIZATION=f"Bearer {key}")

    def post(self, name, key, body):
        if name == "activation":
            self._prove(key, body.get("account", ""), lid=body.get("lid", ""))
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
        self.assertRegex(lic.org_id, r"^ORG-\d{5}$")
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
        customer = services.get_or_create_customer("Test@Acme.co", org="Acme")
        r = self.client.post(reverse("admin:licensing_license_add"),
                             {"customer": customer.pk, "tier": tier.pk, "status": "active", "months": 12})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        license_ = License.objects.get(customer__email="test@acme.co")
        self.assertEqual(license_.tier, tier)
        self.assertEqual(license_.provider, "manual")
        r = self.get("account_license", self.safety_key, email="test@acme.co")
        self.assertEqual(r.json()["license"]["plan"], "safety-t3")

    def test_add_page_picks_a_customer(self):
        page = self.client.get(reverse("admin:licensing_license_add")).content.decode()
        self.assertIn('name="customer"', page)
        self.assertNotIn('name="email"', page)

    def test_org_id_is_given_once_and_never_changes(self):
        org = Organisation.objects.create(name="Acme")
        self.assertRegex(org.org_id, r"^ORG-\d{5}$")
        org.name = "Acme Mining"
        org.save()   # the name may change
        org.org_id = "ORG-99999"
        with self.assertRaises(ValueError):
            org.save()
        with self.assertRaises(ValueError):
            org.delete()
        second = Organisation.objects.create(name="Acme")   # same name, its own ID
        self.assertNotEqual(second.org_id, Organisation.objects.get(pk=org.pk).org_id)
        page = self.client.get(reverse("admin:licensing_organisation_change", args=[org.pk])).content.decode()
        self.assertNotIn('name="org_id"', page)
        self.assertEqual(self.client.get(reverse("admin:licensing_organisation_delete", args=[org.pk])).status_code,
                         403)

    def test_one_organisation_for_both_apps(self):
        org = Organisation.objects.create(name="Acme")
        a = services.get_or_create_customer("boss@acme.co", org_id=org.org_id.lower())
        b = services.get_or_create_customer("finance@acme.co", org_id=org.org_id)
        self.assertEqual(a.organisation, org)
        self.assertEqual(b.organisation, org)
        safety = self.get("account_license", self.safety_key, email="boss@acme.co").json()["license"]
        cashflow = self.get("account_license", self.cashflow_key, email="finance@acme.co").json()["license"]
        self.assertEqual(safety["org_id"], org.org_id)
        self.assertEqual(cashflow["org_id"], org.org_id)
        self.assertEqual(verify(cashflow["certificate"], self.public_keys(), product="pi-cashflow").org_id, org.org_id)
        services.get_or_create_customer("boss@acme.co", org="Other Co")   # an organisation is kept, not replaced
        self.assertEqual(Customer.objects.get(email="boss@acme.co").organisation, org)

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

    def test_download_certificate_action(self):
        lic = self.paid_license("p@co.za", "safety-t2")
        r = self.client.post(reverse("admin:licensing_license_changelist"),
                             {"action": "download_certificate", "_selected_action": [lic.pk]})
        self.assertIn(".pilic", r["Content-Disposition"])
        self.assertEqual(verify(r.content.decode(), self.public_keys()).plan, "safety-t2")

    def test_revoke_and_reactivate(self):
        lic = self.paid_license("p@co.za", "cashflow-t4")
        url = reverse("admin:licensing_license_changelist")
        self.client.post(url, {"action": "revoke", "_selected_action": [lic.pk]})
        self.assertEqual(self.get("account_license", self.cashflow_key, email="p@co.za").json()["license"]["plan"],
                         "cashflow-t1")
        self.client.post(url, {"action": "reactivate", "_selected_action": [lic.pk]})
        self.assertEqual(self.get("account_license", self.cashflow_key, email="p@co.za").json()["license"]["plan"],
                         "cashflow-t4")


class OneAccountPerLicenceTests(LicensingTestCase):
    """A licence belongs to one app account; an email can hold several; the administrator picks."""

    def test_second_account_refused(self):
        paid = self.paid_license("boss@co.za", "cashflow-t3")
        self.assertEqual(self.post("activation", self.cashflow_key, {"lid": paid.lid, "account": "acme"}).status_code, 201)
        # the same account again is fine
        self.assertEqual(self.post("activation", self.cashflow_key, {"lid": paid.lid, "account": "acme"}).status_code, 201)
        r = self.post("activation", self.cashflow_key, {"lid": paid.lid, "account": "other"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "in_use")
        self.assertEqual(Activation.objects.get().account_ref, "acme")

    def test_same_account_name_in_another_installation_refused(self):
        paid = self.paid_license("boss@co.za", "cashflow-t3")
        _, second_install = AppClient.create_with_key("Cash Flow, second Mac", self.cashflow)
        self.post("activation", self.cashflow_key, {"lid": paid.lid, "account": "acme"})
        r = self.post("activation", second_install, {"lid": paid.lid, "account": "acme"})
        self.assertEqual(r.status_code, 409)

    def test_certificate_names_the_account_once_tied(self):
        paid = self.paid_license("boss@co.za", "cashflow-t3")
        before = verify(services.describe(paid)["certificate"], self.public_keys(), product="pi-cashflow")
        self.assertEqual(before.account, "")
        data = self.post("activation", self.cashflow_key, {"lid": paid.lid, "account": "acme"}).json()["license"]
        lic = verify(data["certificate"], self.public_keys(), product="pi-cashflow")
        self.assertEqual(lic.account, "acme")
        self.assertEqual(lic.installation, self.cashflow_key.split("_")[1])
        self.assertTrue(lic.belongs_elsewhere("other"))
        self.assertFalse(lic.belongs_elsewhere("acme", self.cashflow_key.split("_")[1]))
        self.assertTrue(lic.belongs_elsewhere("acme", "otherinstall"))

    def test_list_shows_licences_and_who_uses_them(self):
        a = self.paid_license("reseller@co.za", "cashflow-t3")
        b = self.paid_license("reseller@co.za", "cashflow-t2", days_left=100)
        self.post("activation", self.cashflow_key, {"lid": a.lid, "account": "client1"})
        r = self.get("licenses_for_email", self.cashflow_key, email="reseller@co.za", account="client2")
        self.assertEqual(r.status_code, 200)
        rows = {x["lid"]: x for x in r.json()["licenses"]}
        self.assertEqual(rows[a.lid]["account"], "other")
        self.assertEqual(rows[b.lid]["account"], "")
        self.assertNotIn("certificate", rows[b.lid])
        free = [x for x in rows.values() if x["free"]]
        self.assertEqual(len(free), 1)   # a free licence this account could use is always offered
        r = self.get("licenses_for_email", self.cashflow_key, email="reseller@co.za", account="client1")
        self.assertEqual({x["lid"]: x for x in r.json()["licenses"]}[a.lid]["account"], "this")

    def test_account_falls_back_to_its_own_free_licence(self):
        paid = self.paid_license("boss@co.za", "cashflow-t3")  # a paid licence under the email is never picked
        r = self.post("free_license", self.cashflow_key, {"email": "boss@co.za", "product": "pi-cashflow",
                                                          "account": "acme"})
        data = r.json()["license"]
        self.assertTrue(data["free"])
        self.assertNotEqual(data["lid"], paid.lid)
        self.assertEqual(verify(data["certificate"], self.public_keys(), product="pi-cashflow").account, "acme")
        again = self.post("free_license", self.cashflow_key, {"email": "boss@co.za", "product": "pi-cashflow",
                                                              "account": "acme"}).json()["license"]
        self.assertEqual(again["lid"], data["lid"])
        other = self.post("free_license", self.cashflow_key, {"email": "boss@co.za", "product": "pi-cashflow",
                                                              "account": "second"}).json()["license"]
        self.assertNotEqual(other["lid"], data["lid"])

    def test_licences_found_by_org_id(self):
        org = Organisation.objects.create(name="Acme")
        boss = services.get_or_create_customer("boss@acme.co", org_id=org.org_id)
        services.get_or_create_customer("finance@acme.co", org_id=org.org_id)
        a = self.paid_license("boss@acme.co", "cashflow-t3")
        b = self.paid_license("finance@acme.co", "cashflow-t2")
        self.paid_license("someone@else.co", "cashflow-t3")   # another organisation
        self.paid_license("boss@acme.co", "safety-t3")       # another product
        self.post("activation", self.cashflow_key, {"lid": a.lid, "account": "acme"})
        r = self.get("licenses_for_email", self.cashflow_key, org_id=org.org_id.lower(), account="second")
        rows = {x["lid"]: x for x in r.json()["licenses"]}
        self.assertEqual(set(rows), {a.lid, b.lid})
        self.assertEqual((rows[a.lid]["account"], rows[b.lid]["account"]), ("other", ""))
        self.assertEqual(rows[b.lid]["org_id"], org.org_id)
        self.assertEqual(boss.organisation, org)
        self.assertEqual(self.get("licenses_for_email", self.cashflow_key, org_id="ORG-99999",
                                  account="x").status_code, 404)

    def test_activation_refuses_another_organisations_licence(self):
        acme = Organisation.objects.create(name="Acme")
        other = Organisation.objects.create(name="Other")
        services.get_or_create_customer("boss@acme.co", org_id=acme.org_id)
        lic = self.paid_license("boss@acme.co", "cashflow-t3")
        r = self.post("activation", self.cashflow_key, {"lid": lic.lid, "account": "x", "org_id": other.org_id})
        self.assertEqual((r.status_code, r.json()["error"]), (409, "other_org"))
        r = self.post("activation", self.cashflow_key, {"lid": lic.lid, "account": "x", "org_id": acme.org_id})
        self.assertEqual(r.status_code, 201)

    def test_organisation_lookup(self):
        org = Organisation.objects.create(name="Acme")
        r = self.get("organisation", self.cashflow_key, url_kwargs={"org_id": org.org_id.lower()}, account="acme")
        self.assertEqual(r.status_code, 404)  # not linked: Org IDs count up, so names are not handed out
        self._link(self.cashflow_key, "acme", org.org_id)
        r = self.get("organisation", self.cashflow_key, url_kwargs={"org_id": org.org_id.lower()}, account="acme")
        self.assertEqual(r.json()["organisation"], {"org_id": org.org_id, "name": "Acme"})
        self.assertEqual(self.get("organisation", self.cashflow_key, url_kwargs={"org_id": "ORG-99999"}).status_code,
                         404)

    def test_list_needs_the_account(self):
        r = self.get("licenses_for_email", self.cashflow_key, email="x@co.za")
        self.assertEqual(r.status_code, 400)

    def test_each_account_gets_its_own_free_licence(self):
        r1 = self.get("account_license", self.cashflow_key, email="one@co.za", account="acc1").json()["license"]
        r2 = self.get("account_license", self.cashflow_key, email="one@co.za", account="acc2").json()["license"]
        self.assertTrue(r1["free"] and r2["free"])
        self.assertNotEqual(r1["lid"], r2["lid"])
        again = self.get("account_license", self.cashflow_key, email="one@co.za", account="acc1").json()["license"]
        self.assertEqual(again["lid"], r1["lid"])

    def test_email_lookup_skips_paid_licence_used_elsewhere(self):
        paid = self.paid_license("boss@co.za", "cashflow-t3")
        self.post("activation", self.cashflow_key, {"lid": paid.lid, "account": "acme"})
        r = self.get("account_license", self.cashflow_key, email="boss@co.za", account="second").json()["license"]
        self.assertTrue(r["free"])
        r = self.get("account_license", self.cashflow_key, email="boss@co.za", account="acme").json()["license"]
        self.assertEqual(r["lid"], paid.lid)

    def test_lapsed_licence_moves_its_account_to_a_free_one(self):
        paid = self.paid_license("boss@co.za", "cashflow-t3")
        self.post("activation", self.cashflow_key, {"lid": paid.lid, "account": "acme"})
        License.objects.filter(pk=paid.pk).update(period_end=timezone.now() - timedelta(days=30))
        r = self.get("license_status", self.cashflow_key, url_kwargs={"lid": paid.lid}).json()
        self.assertFalse(r["usable"])
        self.assertTrue(r["current"]["free"])
        lic = verify(r["current"]["certificate"], self.public_keys(), product="pi-cashflow")
        self.assertEqual(lic.account, "acme")

    def test_released_licence_can_move(self):
        paid = self.paid_license("boss@co.za", "cashflow-t3")
        self.post("activation", self.cashflow_key, {"lid": paid.lid, "account": "acme"})
        Activation.objects.filter(license=paid).delete()   # "Delete" in the admin
        self.assertEqual(self.post("activation", self.cashflow_key, {"lid": paid.lid, "account": "new"}).status_code, 201)
        data = services.describe(paid)
        self.assertEqual(verify(data["certificate"], self.public_keys(), product="pi-cashflow").account, "new")


class ProofTests(LicensingTestCase):
    """An app account only sees and uses licences it has proved: an email confirmed with an emailed code, the
    organisation it is linked to, or the licence's certificate."""

    prove = False

    def code_for(self, email, account="acme", key=None):
        from django.core import mail

        mail.outbox.clear()
        r = self.post("email_code", key or self.cashflow_key, {"email": email, "account": account,
                                                                "account_name": "Acme Mining", "requested_by": "Jo"})
        self.assertEqual(r.status_code, 202, r.content)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [services.normalise_email(email)])
        import re

        return re.search(r"\b(\d{6})\b", message.subject).group(1), message

    def confirm(self, email, account="acme", key=None):
        code, _ = self.code_for(email, account, key)
        r = self.post("email_code_check", key or self.cashflow_key, {"email": email, "account": account, "code": code})
        self.assertEqual(r.status_code, 200, r.content)

    def test_email_lookup_needs_a_code(self):
        r = self.get("licenses_for_email", self.cashflow_key, email="guess@co.za", account="acme")
        self.assertEqual((r.status_code, r.json()["error"]), (403, "not_proven"))
        self.assertFalse(Customer.objects.filter(email="guess@co.za").exists())  # nothing made for a guess
        r = self.get("account_license", self.cashflow_key, email="guess@co.za", account="acme")
        self.assertEqual(r.status_code, 403)

    def test_code_email_names_account_and_asker_and_is_stored_hashed(self):
        code, message = self.code_for("Boss@Acme.co")
        self.assertIn("Acme Mining", message.body)
        self.assertIn("Jo", message.body)
        self.assertIn("15 minutes", message.body)
        from .models import EmailCode

        self.assertNotIn(code, EmailCode.objects.get().code_hash)

    def test_right_code_lists_only_this_products_licences(self):
        cash = self.paid_license("boss@acme.co", "cashflow-t3")
        self.paid_license("boss@acme.co", "safety-t3")
        self.confirm("boss@acme.co")
        r = self.get("licenses_for_email", self.cashflow_key, email="boss@acme.co", account="acme")
        rows = r.json()["licenses"]
        self.assertTrue(rows)
        self.assertEqual({x["product"] for x in rows}, {"pi-cashflow"})
        self.assertIn(cash.lid, [x["lid"] for x in rows])
        self.assertEqual({x["email"] for x in rows}, {"boss@acme.co"})
        # the proof is for this account and this app only
        self.assertEqual(self.get("licenses_for_email", self.cashflow_key, email="boss@acme.co",
                                  account="other").status_code, 403)
        self.assertEqual(self.get("licenses_for_email", self.safety_key, email="boss@acme.co",
                                  account="acme").status_code, 403)

    def test_wrong_codes_then_locked(self):
        code, _ = self.code_for("boss@acme.co")
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(4):
            r = self.post("email_code_check", self.cashflow_key, {"email": "boss@acme.co", "account": "acme",
                                                                   "code": wrong})
            self.assertEqual(r.json()["error"], "bad_code")
        r = self.post("email_code_check", self.cashflow_key, {"email": "boss@acme.co", "account": "acme",
                                                               "code": wrong})
        self.assertEqual(r.status_code, 429)
        r = self.post("email_code_check", self.cashflow_key, {"email": "boss@acme.co", "account": "acme",
                                                               "code": code})
        self.assertEqual(r.status_code, 400)  # void after too many tries: a new code is needed

    def test_code_for_another_account_does_not_work(self):
        code, _ = self.code_for("boss@acme.co", account="acme")
        r = self.post("email_code_check", self.cashflow_key, {"email": "boss@acme.co", "account": "thief",
                                                               "code": code})
        self.assertEqual(r.status_code, 400)

    def test_new_code_replaces_old_and_codes_expire(self):
        old, _ = self.code_for("boss@acme.co")
        new, _ = self.code_for("boss@acme.co")
        if old != new:
            r = self.post("email_code_check", self.cashflow_key, {"email": "boss@acme.co", "account": "acme",
                                                                   "code": old})
            self.assertEqual(r.status_code, 400)
        from .models import EmailCode

        EmailCode.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        r = self.post("email_code_check", self.cashflow_key, {"email": "boss@acme.co", "account": "acme",
                                                               "code": new})
        self.assertEqual(r.status_code, 400)

    @override_settings(LICENSE_CODES_PER_HOUR=2)
    def test_too_many_codes(self):
        self.code_for("boss@acme.co")
        self.code_for("boss@acme.co")
        r = self.post("email_code", self.cashflow_key, {"email": "boss@acme.co", "account": "acme"})
        self.assertEqual((r.status_code, r.json()["error"]), (429, "too_many"))

    def test_proof_runs_out(self):
        self.confirm("boss@acme.co")
        EmailProof.objects.update(verified_at=timezone.now() - timedelta(hours=25))
        self.assertEqual(self.get("licenses_for_email", self.cashflow_key, email="boss@acme.co",
                                  account="acme").status_code, 403)

    def test_activation_needs_proof_or_certificate(self):
        lic = self.paid_license("boss@acme.co", "cashflow-t3")
        r = self.post("activation", self.cashflow_key, {"lid": lic.lid, "account": "thief"})
        self.assertEqual((r.status_code, r.json()["error"]), (403, "not_proven"))
        self.assertFalse(Activation.objects.exists())
        other = self.paid_license("x@co.za", "cashflow-t3")
        cert = services.issue_certificate(other).token
        r = self.post("activation", self.cashflow_key, {"lid": lic.lid, "account": "thief", "certificate": cert})
        self.assertEqual(r.status_code, 403)  # a certificate of another licence proves nothing
        cert = services.issue_certificate(lic).token
        r = self.post("activation", self.cashflow_key, {"lid": lic.lid, "account": "acme", "certificate": cert})
        self.assertEqual(r.status_code, 201, r.content)

    def test_activation_after_code(self):
        lic = self.paid_license("boss@acme.co", "cashflow-t3")
        self.confirm("boss@acme.co")
        r = self.post("activation", self.cashflow_key, {"lid": lic.lid, "account": "acme"})
        self.assertEqual(r.status_code, 201)

    def test_org_needs_a_confirmed_email_of_it(self):
        acme = Organisation.objects.create(name="Acme")
        services.get_or_create_customer("boss@acme.co", org_id=acme.org_id)
        lic = self.paid_license("finance@acme.co", "cashflow-t3")
        lic.customer.organisation = acme
        lic.customer.save()
        r = self.get("licenses_for_email", self.cashflow_key, org_id=acme.org_id, account="acme")
        self.assertEqual((r.status_code, r.json()["error"]), (403, "org_not_linked"))
        r = self.post("org_link", self.cashflow_key, {"org_id": acme.org_id, "account": "acme"})
        self.assertEqual((r.status_code, r.json()["error"]), (403, "not_proven"))
        self.confirm("outsider@else.co")  # an email outside the organisation does not count
        self.assertEqual(self.post("org_link", self.cashflow_key, {"org_id": acme.org_id,
                                                                   "account": "acme"}).status_code, 403)
        self.confirm("boss@acme.co")
        r = self.post("org_link", self.cashflow_key, {"org_id": acme.org_id, "account": "acme"})
        self.assertEqual(r.json()["organisation"]["org_id"], acme.org_id)
        r = self.get("licenses_for_email", self.cashflow_key, org_id=acme.org_id, account="acme")
        self.assertEqual([x["lid"] for x in r.json()["licenses"]], [lic.lid])
        # linked: the organisation's licences may be used without confirming each email
        self.assertEqual(self.post("activation", self.cashflow_key, {"lid": lic.lid, "account": "acme"}).status_code,
                         201)

    def test_using_an_org_licence_links_the_account(self):
        acme = Organisation.objects.create(name="Acme")
        services.get_or_create_customer("boss@acme.co", org_id=acme.org_id)
        lic = self.paid_license("boss@acme.co", "cashflow-t3")
        self.confirm("boss@acme.co")
        self.post("activation", self.cashflow_key, {"lid": lic.lid, "account": "acme"})
        self.assertTrue(OrgLink.objects.filter(account_ref="acme", organisation=acme).exists())

    def test_free_licence_ignores_an_unlinked_org(self):
        acme = Organisation.objects.create(name="Acme")
        r = self.post("free_license", self.cashflow_key, {"email": "new@x.co", "account": "x", "org_id": acme.org_id})
        self.assertEqual(r.status_code, 201)
        self.assertIsNone(Customer.objects.get(email="new@x.co").organisation)

    def test_website_key_needs_no_proof(self):
        self.paid_license("boss@acme.co", "cashflow-t3")
        r = self.get("account_license", self.website_key, email="boss@acme.co", product="pi-cashflow")
        self.assertEqual(r.status_code, 200)
