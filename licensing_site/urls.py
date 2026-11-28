from django.contrib import admin
from django.urls import path

from licensing import store, views

admin.site.site_header = admin.site.site_title = "PI - Licensing Service"

urlpatterns = [
    path(".well-known/pi-license-keys", views.public_keys, name="public_keys"),
    path("v1/accounts/licenses", views.account_license, name="account_license"),
    path("v1/free-licenses", views.free_license, name="free_license"),
    path("v1/licenses", views.licenses_for_email, name="licenses_for_email"),
    path("v1/organisations/<str:org_id>", views.organisation, name="organisation"),
    path("v1/licenses/<str:lid>/status", views.license_status, name="license_status"),
    path("v1/licenses/<str:lid>/certificate", views.license_certificate, name="license_certificate"),
    path("v1/activations", views.activation, name="activation"),
    path("v1/email-codes", views.email_code, name="email_code"),
    path("v1/email-codes/check", views.email_code_check, name="email_code_check"),
    path("v1/org-links", views.org_link, name="org_link"),
    path("store/", store.index, name="store"),
    path("store/thanks/", store.thanks, name="store_thanks"),
    path("store/<slug:tier_code>/", store.buy, name="store_buy"),
    path("webhooks/paddle", store.paddle_webhook, name="paddle_webhook"),
    path("admin/", admin.site.urls),
]
