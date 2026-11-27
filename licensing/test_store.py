import hashlib
import hmac
import json
import time
from datetime import timedelta

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import paddle
from .models import Customer, License, Organisation, Product, Tier, WebhookEvent

SECRET = "pdl_ntfset_test_secret"


def signed(body: bytes, secret=SECRET, ts=None):
    ts = int(ts if ts is not None else time.time())
    h1 = hmac.new(secret.encode(), f"{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return f"ts={ts};h1={h1}"


def sub_event(event_id, kind="subscription.created", sub="sub_01", price="pri_t2", status="active",
              start="2026-11-27T10:00:00Z", end="2027-11-27T10:00:00Z", occurred="2026-11-27T10:00:05Z",
              custom=None, customer_id="ctm_01", **extra):
    data = {"id": sub, "status": status, "customer_id": customer_id,
            "items": [{"price": {"id": price}, "quantity": 1}],
            "current_billing_period": {"starts_at": start, "ends_at": end} if start else None,
            "custom_data": custom if custom is not None else {
                "email": "Buyer@Example.com", "name": "Thandi Buyer", "org": "Acme Mining", "org_id": "",
                "tier": "safety-t2"}}
    data.update(extra)
    return {"event_id": event_id, "event_type": kind, "occurred_at": occurred, "data": data}


@override_settings(PADDLE_WEBHOOK_SECRET=SECRET, PADDLE_CLIENT_TOKEN="test_token", PADDLE_ENV="sandbox",
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class StoreTests(TestCase):
    def setUp(self):
        self.t2 = Tier.objects.get(code="safety-t2")
        self.t3 = Tier.objects.get(code="safety-t3")
        Tier.objects.filter(pk=self.t2.pk).update(provider_price_id="pri_t2")
        Tier.objects.filter(pk=self.t3.pk).update(provider_price_id="pri_t3")

    def post(self, payload, header=None):
        body = json.dumps(payload).encode()
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse("paddle_webhook"), body, content_type="application/json",
                                    HTTP_PADDLE_SIGNATURE=header if header is not None else signed(body))

    # Store pages
    def test_store_lists_plans_from_the_tiers(self):
        page = self.client.get(reverse("store")).content.decode()
        self.assertIn("Safety Compliance Insight", page)
        self.assertIn("US$198", page)
        self.assertIn("Clients, business units and sites", page)
        self.assertIn(reverse("store_buy", args=["safety-t2"]), page)
        self.assertIn("Coming Soon", page)  # Cash Flow tiers have no Paddle price yet

    @override_settings(PADDLE_CLIENT_TOKEN="")
    def test_store_without_paddle_is_coming_soon(self):
        page = self.client.get(reverse("store")).content.decode()
        self.assertIn("Online payments are coming soon", page)
        self.assertNotIn(reverse("store_buy", args=["safety-t2"]), page)

    def test_free_tier_cannot_be_bought(self):
        self.assertEqual(self.client.get(reverse("store_buy", args=["safety-t1"])).status_code, 404)

    def test_details_form_opens_checkout_with_custom_data(self):
        org = Organisation.objects.create(name="Acme Mining")
        r = self.client.post(reverse("store_buy", args=["safety-t2"]),
                             {"email": "a@b.co.za", "name": "A B", "org": "", "org_id": org.org_id.lower()})
        page = r.content.decode()
        self.assertIn("cdn.paddle.com/paddle/v2/paddle.js", page)
        self.assertIn('"priceId": "pri_t2"', page)
        self.assertIn(f'"org_id": "{org.org_id}"', page)

    def test_unknown_org_id_or_no_organisation_is_refused(self):
        r = self.client.post(reverse("store_buy", args=["safety-t2"]),
                             {"email": "a@b.co.za", "name": "A B", "org_id": "ORG-99999"})
        self.assertContains(r, "We don&#x27;t know this Org ID")
        r = self.client.post(reverse("store_buy", args=["safety-t2"]), {"email": "a@b.co.za", "name": "A B"})
        self.assertContains(r, "Type your organisation")
        self.assertNotContains(r, "paddle.js")

    # Signatures
    def test_signature_is_checked(self):
        body = b'{"event_id":"evt_1"}'
        paddle.verify_signature(body, signed(body), SECRET)
        for header in ["", signed(body, "wrong"), signed(body, ts=time.time() - 3600), "ts=abc;h1=00"]:
            with self.assertRaises(paddle.BadSignature):
                paddle.verify_signature(body, header, SECRET)
        with self.assertRaises(paddle.BadSignature):
            paddle.verify_signature(body + b" ", signed(body), SECRET)

    def test_webhook_refuses_bad_signature(self):
        r = self.post(sub_event("evt_1"), header="ts=1;h1=bad")
        self.assertEqual(r.status_code, 401)
        self.assertFalse(License.objects.exists())

    @override_settings(PADDLE_WEBHOOK_SECRET="")
    def test_webhook_refused_without_secret(self):
        self.assertEqual(self.post(sub_event("evt_1"), header="ts=1;h1=x").status_code, 401)

    # Licences from subscriptions
    def test_new_subscription_makes_licence_and_emails_buyer(self):
        r = self.post(sub_event("evt_1"))
        self.assertEqual(r.status_code, 200)
        lic = License.objects.get()
        self.assertEqual((lic.tier, lic.status, lic.provider, lic.provider_subscription_id),
                         (self.t2, License.ACTIVE, "paddle", "sub_01"))
        self.assertEqual(lic.period_end.year, 2027)
        customer = lic.customer
        self.assertEqual(customer.email, "buyer@example.com")
        self.assertEqual(customer.organisation.name, "Acme Mining")
        self.assertEqual(customer.provider_customer_id, "ctm_01")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(lic.lid, mail.outbox[0].body)
        self.assertIn("Find Licences", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, ["buyer@example.com"])

    def test_existing_org_id_is_used(self):
        org = Organisation.objects.create(name="Existing Co")
        custom = {"email": "x@y.com", "name": "X", "org": "", "org_id": org.org_id}
        self.post(sub_event("evt_1", custom=custom))
        self.assertEqual(License.objects.get().customer.organisation, org)

    def test_repeated_event_is_applied_once(self):
        self.post(sub_event("evt_1"))
        r = self.post(sub_event("evt_1"))
        self.assertEqual(r.content, b"Already done.")
        self.assertEqual(License.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(WebhookEvent.objects.count(), 1)

    def test_upgrade_renewal_and_overdue_update_the_same_licence(self):
        self.post(sub_event("evt_1"))
        self.post(sub_event("evt_2", "subscription.updated", price="pri_t3", occurred="2026-12-01T00:00:00Z"))
        lic = License.objects.get()
        self.assertEqual(lic.tier, self.t3)
        self.post(sub_event("evt_3", "subscription.past_due", price="pri_t3", status="past_due",
                            occurred="2027-11-28T00:00:00Z", start="2027-11-27T10:00:00Z",
                            end="2028-11-27T10:00:00Z"))
        lic.refresh_from_db()
        self.assertEqual((lic.status, lic.period_end.year), (License.PAST_DUE, 2028))
        self.assertEqual(License.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_older_event_after_newer_is_skipped(self):
        self.post(sub_event("evt_1"))
        self.post(sub_event("evt_3", "subscription.updated", price="pri_t3", occurred="2026-12-02T00:00:00Z"))
        self.post(sub_event("evt_2", "subscription.updated", price="pri_t2", occurred="2026-12-01T00:00:00Z"))
        self.assertEqual(License.objects.get().tier, self.t3)
        self.assertTrue(WebhookEvent.objects.get(event_id="evt_2").result.startswith("Skipped"))

    def test_cancel_runs_to_period_end_then_expires(self):
        self.post(sub_event("evt_1"))
        self.post(sub_event("evt_2", "subscription.canceled", status="canceled", start=None,
                            occurred="2027-11-27T10:00:01Z", canceled_at="2027-11-27T10:00:00Z"))
        lic = License.objects.get()
        self.assertEqual(lic.status, License.CANCELED)
        self.assertEqual(lic.period_end.year, 2027)

    def test_immediate_cancel_ends_now(self):
        self.post(sub_event("evt_1"))
        self.post(sub_event("evt_2", "subscription.canceled", status="canceled", start=None,
                            occurred="2027-01-10T10:00:01Z", canceled_at="2027-01-10T10:00:00Z"))
        lic = License.objects.get()
        self.assertEqual((lic.period_end.year, lic.period_end.month), (2027, 1))

    def test_revoked_licence_stays_revoked(self):
        self.post(sub_event("evt_1"))
        License.objects.update(status=License.REVOKED)
        self.post(sub_event("evt_2", "subscription.updated", occurred="2026-12-01T00:00:00Z"))
        self.assertEqual(License.objects.get().status, License.REVOKED)

    def test_unknown_price_is_ignored(self):
        r = self.post(sub_event("evt_1", price="pri_other"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(License.objects.exists())
        self.assertIn("not linked to a tier", WebhookEvent.objects.get().result)

    def test_subscription_without_email_waits_for_customer_event(self):
        r = self.post(sub_event("evt_1", custom={}, customer_id="ctm_9"))
        self.assertEqual(r.status_code, 503)
        event = WebhookEvent.objects.get()
        self.assertIsNone(event.processed)
        self.post({"event_id": "evt_c", "event_type": "customer.created", "occurred_at": "2026-11-27T10:00:00Z",
                   "data": {"id": "ctm_9", "email": "late@example.com", "name": "Late"}})
        self.assertTrue(Customer.objects.filter(email="late@example.com", provider_customer_id="ctm_9").exists())
        r = self.post(sub_event("evt_1", custom={}, customer_id="ctm_9"))  # Paddle sends it again
        self.assertEqual(r.status_code, 200)
        self.assertEqual(License.objects.get().customer.email, "late@example.com")

    def test_lapsed_paid_licence_falls_back_to_free(self):
        from . import services
        self.post(sub_event("evt_1", start="2025-01-01T00:00:00Z", end="2025-12-31T00:00:00Z"))
        lic = License.objects.get()
        current = services.current_license(lic.customer, Product.objects.get(code="pi-safety"), timezone.now())
        self.assertTrue(current.is_free)
        lic.refresh_from_db()
        self.assertEqual(lic.status, License.EXPIRED)

    def test_safety_product_renamed(self):
        self.assertEqual(Product.objects.get(code="pi-safety").name, "Safety Compliance Insight")

    def test_thanks_page(self):
        self.assertContains(self.client.get(reverse("store_thanks") + "?plan=safety-t2"), "Use This Licence")
