from django.contrib import admin

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


@admin.register(License)
class LicenseAdmin(admin.ModelAdmin):
    list_display = ["lid", "customer", "product", "tier", "status", "period_end", "provider"]
    list_filter = ["product", "status", "tier"]
    search_fields = ["lid", "customer__email", "provider_subscription_id"]
    readonly_fields = ["lid", "created", "updated"]
    inlines = [CertificateInline, ActivationInline]


@admin.register(AppClient)
class AppClientAdmin(admin.ModelAdmin):
    """Keys are made with `manage.py create_api_key` (the key is shown once); here they can only be switched off."""

    list_display = ["name", "product", "key_prefix", "active", "created"]
    fields = ["name", "product", "key_prefix", "active"]
    readonly_fields = ["key_prefix"]

    def has_add_permission(self, request):
        return False
