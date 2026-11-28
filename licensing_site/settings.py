"""Django settings for PI - Licensing Service.

Same conventions as PI-Cashflow Management and Safety Compliance Insight: every machine-specific value
comes from environment variables or a .env file next to manage.py.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    """Minimal .env loader: KEY=value lines, # comments, no quoting rules."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env_file(BASE_DIR / ".env")


def env(key, default=None):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    return str(env(key, default)).lower() in {"1", "true", "yes", "on"}


APP_NAME = "PI - Licensing Service"
SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = [h.strip() for h in env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

USE_HTTPS = env_bool("USE_HTTPS", False)
if USE_HTTPS:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(env("SECURE_HSTS_SECONDS", "86400"))
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
# Own cookie names: browsers share cookies across ports with the two apps on the same computer.
SESSION_COOKIE_NAME = "pi_licensing_session"
CSRF_COOKIE_NAME = "pi_licensing_csrftoken"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "licensing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "licensing_site.urls"
WSGI_APPLICATION = "licensing_site.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

if env("DB_ENGINE", "postgres") == "sqlite":  # quick local checks only; real installs use PostgreSQL
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "dev.sqlite3"}}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("POSTGRES_DB", "pi_licensing"),
            "USER": env("POSTGRES_USER", os.environ.get("USER", "postgres")),
            "PASSWORD": env("POSTGRES_PASSWORD", ""),
            "HOST": env("POSTGRES_HOST", "localhost"),
            "PORT": env("POSTGRES_PORT", "5432"),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en-za"
TIME_ZONE = "Africa/Johannesburg"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Licensing
# Private signing keys live as <kid>.pem files in SIGNING_KEYS_DIR, readable only by the service's system user;
# SIGNING_KID names the one used for new certificates. Older keys stay so their certificates still verify.
# (A cloud key store can replace the files later without changing anything else.)
LICENSE_SIGNING_KEYS_DIR = Path(env("SIGNING_KEYS_DIR", str(BASE_DIR / "keys")))
LICENSE_SIGNING_KID = env("SIGNING_KID", "")
LICENSE_GRACE_DAYS = int(env("LICENSE_GRACE_DAYS", "7"))
LICENSE_CHECK_HOURS = int(env("LICENSE_CHECK_HOURS", "6"))
FREE_LICENSE_DAYS = int(env("FREE_LICENSE_DAYS", "365"))

# Store and Paddle payments. Without PADDLE_CLIENT_TOKEN the store shows the plans with "Coming soon".
# PADDLE_ENV is "sandbox" for testing or "production". The client-side token is meant for the browser; the
# webhook secret (Paddle → Developer Tools → Notifications) is not, and stays in .env.
PADDLE_ENV = env("PADDLE_ENV", "sandbox")
PADDLE_CLIENT_TOKEN = env("PADDLE_CLIENT_TOKEN", "")
PADDLE_WEBHOOK_SECRET = env("PADDLE_WEBHOOK_SECRET", "")
PADDLE_WEBHOOK_TOLERANCE_SECONDS = int(env("PADDLE_WEBHOOK_TOLERANCE_SECONDS", "300"))
STORE_SUPPORT_EMAIL = env("STORE_SUPPORT_EMAIL", "support@performanceinsight.co.za")
STORE_COMPANY = env("STORE_COMPANY", "Performance Insight")

# Email (licence emails after a purchase). Without EMAIL_HOST emails are printed to the console.
EMAIL_HOST = env("EMAIL_HOST", "")
if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = int(env("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
    EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", STORE_SUPPORT_EMAIL)

# Email codes: an app administrator proves a licence email with a code before its licences are listed or used.
LICENSE_CODE_MINUTES = int(env("LICENSE_CODE_MINUTES", "15"))
LICENSE_CODE_TRIES = int(env("LICENSE_CODE_TRIES", "5"))  # wrong codes before a code is void
LICENSE_CODES_PER_HOUR = int(env("LICENSE_CODES_PER_HOUR", "5"))  # per email
LICENSE_EMAIL_PROOF_HOURS = int(env("LICENSE_EMAIL_PROOF_HOURS", "24"))
