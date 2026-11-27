"""Licensing data. Products and tiers are data, not code, so a new product or tier needs no code change."""

import hashlib
import secrets

from django.db import models, transaction


class Product(models.Model):
    """One app that is licensed, e.g. pi-safety. `limit_names` lists the counters its tiers cap."""

    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=100)
    limit_names = models.JSONField(default=list, help_text='Counter names, e.g. ["users", "cashflow_lines"].')
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Tier(models.Model):
    YEAR = "year"
    MONTH = "month"
    INTERVALS = [(YEAR, "Annual"), (MONTH, "Monthly")]

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="tiers")
    code = models.SlugField(max_length=40, unique=True, help_text="Plan code, e.g. safety-t2.")
    name = models.CharField(max_length=100)
    order = models.PositiveSmallIntegerField(default=1)
    limits = models.JSONField(default=dict, help_text="Counter name to maximum; null means unlimited.")
    features = models.JSONField(default=list, blank=True)
    price_usd = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    interval = models.CharField(max_length=10, choices=INTERVALS, default=YEAR)
    is_free = models.BooleanField(default=False, help_text="Issued on sign-up without payment.")
    provider_price_id = models.CharField(max_length=100, blank=True, help_text="The payment provider's price id.")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["product__name", "order"]
        constraints = [
            models.UniqueConstraint(
                fields=["product"], condition=models.Q(is_free=True), name="one_free_tier_per_product"
            )
        ]

    def __str__(self):
        return f"{self.product.name}: {self.name}"


class Organisation(models.Model):
    """A company that holds licences, for any PI app. Its Org ID (ORG-00001 ...) is given once, when it is first
    saved, from the database's own counter, so it is unique across all products and never reused; it can never be
    changed, and an organisation is never deleted (licences and certificates name it)."""

    org_id = models.CharField("org ID", max_length=20, unique=True, null=True, editable=False)
    name = models.CharField(max_length=200)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["org_id"]
        verbose_name = "organisation"

    def __str__(self):
        return f"{self.org_id} {self.name}" if self.org_id else self.name

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self).objects.filter(pk=self.pk).values_list("org_id", flat=True).first()
            if stored and stored != self.org_id:
                raise ValueError("An organisation's Org ID can never be changed.")
            return super().save(*args, **kwargs)
        with transaction.atomic():
            super().save(*args, **kwargs)
            self.org_id = f"ORG-{self.pk:05d}"
            type(self).objects.filter(pk=self.pk).update(org_id=self.org_id)

    def delete(self, *args, **kwargs):
        raise ValueError("Organisations are never deleted: their Org ID is in licences and certificates.")


class Customer(models.Model):
    """Whoever holds licences, found by email (stored lower case), and the organisation they belong to."""

    email = models.EmailField(unique=True)
    name = models.CharField(max_length=200, blank=True)
    organisation = models.ForeignKey(Organisation, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="customers")
    provider_customer_id = models.CharField(max_length=100, blank=True, db_index=True,
                                            help_text="The payment provider's customer id (Paddle ctm_...).")
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["email"]

    def __str__(self):
        return f"{self.email} ({self.organisation.org_id})" if self.organisation_id else self.email

    @property
    def org_id(self):
        return self.organisation.org_id if self.organisation_id else ""

    @property
    def org_name(self):
        return self.organisation.name if self.organisation_id else ""

    @property
    def public_id(self):
        return f"cus_{self.pk}"


def new_lid():
    return "lic_" + secrets.token_urlsafe(12)


class License(models.Model):
    ACTIVE, PAST_DUE, CANCELED, EXPIRED, REVOKED = "active", "past_due", "canceled", "expired", "revoked"
    STATUSES = [
        (ACTIVE, "Active"),
        (PAST_DUE, "Payment overdue"),
        (CANCELED, "Cancelled (runs to period end)"),
        (EXPIRED, "Expired"),
        (REVOKED, "Revoked"),
    ]
    USABLE = (ACTIVE, PAST_DUE, CANCELED)

    lid = models.CharField(max_length=40, unique=True, default=new_lid)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="licenses")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="licenses")
    tier = models.ForeignKey(Tier, on_delete=models.PROTECT, related_name="licenses")
    status = models.CharField(max_length=20, choices=STATUSES, default=ACTIVE)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    provider = models.CharField(max_length=20, blank=True, help_text="Empty for free licences.")
    provider_subscription_id = models.CharField(max_length=100, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"{self.lid} ({self.tier})"

    @property
    def is_free(self):
        return self.tier.is_free


class Certificate(models.Model):
    """Every signed certificate issued, newest first. The token is the whole JWS."""

    license = models.ForeignKey(License, on_delete=models.CASCADE, related_name="certificates")
    kid = models.CharField(max_length=40)
    plan = models.CharField(max_length=40)
    status = models.CharField(max_length=20)
    issued_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    account_ref = models.CharField(max_length=100, blank=True, help_text="The app account it names, if any.")
    token = models.TextField()

    class Meta:
        ordering = ["-issued_at", "-pk"]


class AppClient(models.Model):
    """A caller of the API: an app's backend (tied to its product) or the website (no product).
    Only a hash of the key is kept; the key itself is shown once when created."""

    name = models.CharField(max_length=100)
    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.PROTECT)
    key_prefix = models.CharField(max_length=12, unique=True)
    key_hash = models.CharField(max_length=64)
    active = models.BooleanField(default=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @staticmethod
    def hash_key(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    @classmethod
    def create_with_key(cls, name, product=None):
        prefix = secrets.token_hex(4)
        key = f"pil_{prefix}_{secrets.token_urlsafe(32)}"
        client = cls.objects.create(name=name, product=product, key_prefix=prefix, key_hash=cls.hash_key(key))
        return client, key

    @classmethod
    def from_key(cls, key: str):
        parts = key.split("_")
        if len(parts) < 3 or parts[0] != "pil":
            return None
        client = cls.objects.filter(key_prefix=parts[1], active=True).select_related("product").first()
        if client and secrets.compare_digest(client.key_hash, cls.hash_key(key)):
            return client
        return None


class Activation(models.Model):
    """The one app account (its schema name, in one app installation = one app key) a licence belongs to.
    A licence is tied to the first account that links or uploads it; other accounts are refused. Deleting the
    activation in the admin releases the licence, so it can be used in another account."""

    license = models.ForeignKey(License, on_delete=models.CASCADE, related_name="activations")
    app_client = models.ForeignKey(AppClient, on_delete=models.PROTECT)
    account_ref = models.CharField(max_length=100)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_seen"]
        constraints = [models.UniqueConstraint(fields=["license"], name="one_account_per_licence")]

    def __str__(self):
        return f"{self.account_ref} ({self.app_client})"

    def is_for(self, app_client, account_ref) -> bool:
        return self.app_client_id == app_client.pk and self.account_ref == account_ref


class WebhookEvent(models.Model):
    """Every payment provider notification received, kept once (by event id) so a repeat is not applied twice."""

    provider = models.CharField(max_length=20)
    event_id = models.CharField(max_length=100)
    event_type = models.CharField(max_length=60)
    occurred_at = models.DateTimeField(null=True, blank=True)
    subscription_id = models.CharField(max_length=100, blank=True, db_index=True)
    received = models.DateTimeField(auto_now_add=True)
    processed = models.DateTimeField(null=True, blank=True)
    result = models.CharField(max_length=300, blank=True)
    payload = models.JSONField(default=dict)

    class Meta:
        ordering = ["-received"]
        constraints = [models.UniqueConstraint(fields=["provider", "event_id"], name="one_event_per_id")]

    def __str__(self):
        return f"{self.event_type} {self.event_id}"
