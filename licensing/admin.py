from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib import admin
from django.http import HttpResponse
from django.utils import timezone

from . import paddle, services

from .models import Activation, AppClient, Certificate, Customer, License, Organisation, Product, Tier, WebhookEvent


class TierInline(admin.TabularInline):
    model = Tier
    extra = 0
    fields = ["order", "code", "name", "limits", "price_usd", "interval", "is_free", "provider_price_id", "active"]


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "limit_names", "active"]
    inlines = [TierInline]


@admin.register(Tier)
class TierAdmin(admin.ModelAdmin):
    list_display = ["product", "order", "name", "code", "limits", "price_usd", "interval", "is_free", "active"]
    list_filter = ["product", "is_free", "active"]


class LicenseInline(admin.TabularInline):
    model = License
    extra = 0
    fields = ["lid", "product", "tier", "status", "period_start", "period_end", "provider"]
    readonly_fields = fields
    can_delete = False
    show_change_link = True


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ["email", "name", "organisation", "created"]
    search_fields = ["email", "name", "organisation__org_id", "organisation__name", "provider_customer_id"]
    autocomplete_fields = ["organisation"]
    inlines = [LicenseInline]


class CustomerInline(admin.TabularInline):
    model = Customer
    extra = 0
    fields = ["email", "name"]
    readonly_fields = fields
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Organisation)
class OrganisationAdmin(admin.ModelAdmin):
    """The Org ID is given when the organisation is first saved and never changes; organisations are never
    deleted (their Org ID is in licences and certificates). The same organisation holds licences for every app."""

    list_display = ["org_id", "name", "customer_count", "created"]
    search_fields = ["org_id", "name"]
    readonly_fields = ["org_id", "created"]
    fields = ["org_id", "name", "created"]
    inlines = [CustomerInline]

    @admin.display(description="customers")
    def customer_count(self, obj):
        return obj.customers.count()

    def has_delete_permission(self, request, obj=None):
        return False


class CertificateInline(admin.TabularInline):
    model = Certificate
    extra = 0
    fields = ["issued_at", "plan", "status", "expires_at", "kid"]
    readonly_fields = fields
    can_delete = False


class ActivationInline(admin.TabularInline):
    """The account the licence belongs to (one at most). Tick Delete and save to release it, so the customer can
    use the licence in another account."""

    model = Activation
    extra = 0
    max_num = 1
    verbose_name = "account using this licence"
    verbose_name_plural = "account using this licence (delete to release it)"
    fields = ["account_ref", "app_client", "first_seen", "last_seen"]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False  # an app ties it when the account links or uploads it


def months_to_days(months):
    return timedelta(days=round(months * 365 / 12))


class IssueLicenceForm(forms.ModelForm):
    """Issue a licence by hand, with no payment: for testing and for special arrangements. The customer is picked
    from the customers (add a new one with the + next to the list); their organisation goes with them."""

    months = forms.IntegerField(min_value=1, max_value=60, initial=12, help_text="Length of the period.")

    class Meta:
        model = License
        fields = ["customer", "tier", "status"]

    def clean(self):
        data = super().clean()
        tier, customer = data.get("tier"), data.get("customer")
        if tier and tier.is_free and customer and License.objects.filter(
                customer=customer, product=tier.product, tier__is_free=True).exists():
            raise forms.ValidationError("This customer already has the free tier for that product.")
        return data

    def save(self, commit=True):
        tier = self.cleaned_data["tier"]
        customer = self.cleaned_data["customer"]
        now = timezone.now()
        license_ = self.instance
        license_.customer = customer
        license_.product = tier.product
        license_.period_start = now
        license_.period_end = now + months_to_days(self.cleaned_data["months"])
        license_.provider = "" if tier.is_free else services.MANUAL_PROVIDER
        return super().save(commit)


@admin.register(License)
class LicenseAdmin(admin.ModelAdmin):
    list_display = ["lid", "customer", "organisation", "product", "tier", "status", "period_end", "provider",
                    "used_by"]
    list_filter = ["product", "status", "tier"]
    search_fields = ["lid", "customer__email", "customer__organisation__org_id", "customer__organisation__name",
                     "provider_subscription_id"]
    readonly_fields = ["lid", "created", "updated"]
    autocomplete_fields = ["customer"]

    @admin.display(description="organisation", ordering="customer__organisation__org_id")
    def organisation(self, obj):
        return obj.customer.organisation or "–"
    inlines = [CertificateInline, ActivationInline]

    @admin.display(description="Used by account")
    def used_by(self, obj):
        act = obj.activations.first()
        return f"{act.account_ref} ({act.app_client})" if act else "–"
    actions = ["download_certificate", "expire_now", "revoke", "reactivate", "extend_year"]

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs["form"] = IssueLicenceForm
            kwargs["fields"] = None
        return super().get_form(request, obj, **kwargs)

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return [(None, {"fields": ["customer", "tier", "status", "months"],
                            "description": "Issues a licence with no payment. Use it to test the apps or for "
                                           "special arrangements."})]
        return super().get_fieldsets(request, obj)

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields if obj else []

    def get_inlines(self, request, obj):
        return self.inlines if obj else []

    @admin.action(description="Download certificate (.pilic), to test uploading it in an app")
    def download_certificate(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Choose one licence to download its certificate.", level="warning")
            return None
        license_ = services.refresh(queryset.get())
        response = HttpResponse(services.describe(license_)["certificate"], content_type="application/x-pi-license")
        response["Content-Disposition"] = f'attachment; filename="{license_.product.code}-{license_.lid}.pilic"'
        return response

    @admin.action(description="Expire now (customer drops to the free tier)")
    def expire_now(self, request, queryset):
        past = timezone.now() - timedelta(days=settings.LICENSE_GRACE_DAYS + 1)
        n = queryset.filter(tier__is_free=False).update(status=License.EXPIRED, period_end=past)
        self.message_user(request, f"{n} paid licence(s) expired. Free licences cannot be expired.")

    @admin.action(description="Revoke")
    def revoke(self, request, queryset):
        n = queryset.update(status=License.REVOKED)
        self.message_user(request, f"{n} licence(s) revoked.")

    @admin.action(description="Make active again")
    def reactivate(self, request, queryset):
        now = timezone.now()
        for license_ in queryset:
            license_.status = License.ACTIVE
            if license_.period_end <= now:
                license_.period_start = now
                license_.period_end = now + timedelta(days=365)
            license_.save()
        self.message_user(request, "Licence(s) active again.")

    @admin.action(description="Extend by a year")
    def extend_year(self, request, queryset):
        for license_ in queryset:
            license_.period_end = max(license_.period_end, timezone.now()) + timedelta(days=365)
            if license_.status == License.EXPIRED:
                license_.status = License.ACTIVE
            license_.save()
        self.message_user(request, "Licence(s) extended by a year.")


@admin.register(AppClient)
class AppClientAdmin(admin.ModelAdmin):
    """Keys are made with `manage.py create_api_key` (the key is shown once); here they can only be switched off."""

    list_display = ["name", "product", "key_prefix", "active", "created"]
    fields = ["name", "product", "key_prefix", "active"]
    readonly_fields = ["key_prefix"]

    def has_add_permission(self, request):
        return False


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    """Payment notifications as received (read only). "Waiting" or "Failed" ones can be processed again."""

    list_display = ["received", "event_type", "subscription_id", "processed", "result"]
    list_filter = ["provider", "event_type", ("processed", admin.EmptyFieldListFilter)]
    search_fields = ["event_id", "subscription_id", "result"]
    readonly_fields = ["provider", "event_id", "event_type", "occurred_at", "subscription_id", "received",
                       "processed", "result", "payload"]
    actions = ["process_again"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Process again")
    def process_again(self, request, queryset):
        for event in queryset.order_by("occurred_at", "received"):
            try:
                result = paddle.process(event)
            except Exception as exc:  # noqa: BLE001 - show the reason to the admin
                result = f"Failed: {exc}"
            self.message_user(request, f"{event.event_type} {event.event_id}: {result}")
