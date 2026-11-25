from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib import admin
from django.utils import timezone

from . import services

from .models import Activation, AppClient, Certificate, Customer, License, Product, Tier


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
    list_display = ["email", "name", "org", "created"]
    search_fields = ["email", "name", "org"]
    inlines = [LicenseInline]


class CertificateInline(admin.TabularInline):
    model = Certificate
    extra = 0
    fields = ["issued_at", "plan", "status", "expires_at", "kid"]
    readonly_fields = fields
    can_delete = False


class ActivationInline(admin.TabularInline):
    model = Activation
    extra = 0
    fields = ["account_ref", "app_client", "first_seen", "last_seen"]
    readonly_fields = fields


def months_to_days(months):
    return timedelta(days=round(months * 365 / 12))


class IssueLicenceForm(forms.ModelForm):
    """Issue a licence by hand, with no payment: for testing and for special arrangements."""

    email = forms.EmailField(help_text="The customer's email; they are created if new.")
    org = forms.CharField(label="Organisation", required=False)
    months = forms.IntegerField(min_value=1, max_value=60, initial=12, help_text="Length of the period.")

    class Meta:
        model = License
        fields = ["tier", "status"]

    def clean(self):
        data = super().clean()
        tier = data.get("tier")
        if tier and tier.is_free and data.get("email"):
            customer = Customer.objects.filter(email=services.normalise_email(data["email"])).first()
            if customer and License.objects.filter(customer=customer, product=tier.product,
                                                   tier__is_free=True).exists():
                raise forms.ValidationError("This customer already has the free tier for that product.")
        return data

    def save(self, commit=True):
        tier = self.cleaned_data["tier"]
        customer = services.get_or_create_customer(self.cleaned_data["email"], org=self.cleaned_data["org"])
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
    list_display = ["lid", "customer", "product", "tier", "status", "period_end", "provider"]
    list_filter = ["product", "status", "tier"]
    search_fields = ["lid", "customer__email", "provider_subscription_id"]
    readonly_fields = ["lid", "created", "updated"]
    inlines = [CertificateInline, ActivationInline]
    actions = ["expire_now", "revoke", "reactivate", "extend_year"]

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs["form"] = IssueLicenceForm
            kwargs["fields"] = None
        return super().get_form(request, obj, **kwargs)

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return [(None, {"fields": ["email", "org", "tier", "status", "months"],
                            "description": "Issues a licence with no payment. Use it to test the apps or for "
                                           "special arrangements."})]
        return super().get_fieldsets(request, obj)

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields if obj else []

    def get_inlines(self, request, obj):
        return self.inlines if obj else []

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
