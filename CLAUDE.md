# PI - Licensing Service

Licences for the PI apps (PI - Safety Compliance Tracker, PI-Cashflow Management, later products).
Owner: Johann Kruger (Simpology). Design: https://claude.ai/artifact/VJAm4jcwHu1XLs9AEoE3hL

## Stack and how to run

- Django 5.2, PostgreSQL 16, waitress + whitenoise, same conventions as the two apps (`.env`, `start.sh`).
  No django-tenants: this service has one shared database `pi_licensing`.
- Port **8002** (Cash Flow 8000, Safety Compliance 8001).
- Python 3.11+: always `.venv/bin/python manage.py ...`; `./start.sh --install` makes the venv (on macOS in
  `.venv.nosync`, so iCloud leaves it alone). Tests: `manage.py test licensing` and the package tests
  (`cd package && python -m unittest discover -s tests`); all must pass before a commit.
  `DB_ENGINE=sqlite` runs them without PostgreSQL for a quick check only.

## Rules

- `package/src/pi_license` (src layout, so a stray `pi_license` folder can't hide it) is installed by both apps:
  keep it dependency-light (cryptography only, standard library for HTTP) and backwards compatible. Certificate format changes bump `SCHEMA_VERSION`
  (optional claims left out when empty, like `account`/`installation`, don't: older packages ignore them).
  Bump the package version in `package/pyproject.toml` with every change, and the apps pin a commit in requirements.
- One licence, one app account: `Activation` (unique per licence, app client + schema name); `services.InUse` when
  another account has it. Free licences are per account. Certificates name the account once tied.
- Organisations (`Organisation`): `org_id` ORG-<pk, 5 digits> is set once on the first save from the primary key,
  so it is unique across products and never reused; `save()` refuses a change and `delete()` refuses always (admin
  too; Customer FK is PROTECT). Customers belong to one organisation; certificates carry `org` (name) and `org_id`
  (optional claim). Manual licences in the admin pick a Customer (autocomplete), never type an email.
- Products and tiers are data (`Product`, `Tier`), never code. The agreed tiers are in `licensing/catalogue.py`,
  loaded by migration 0002; later changes are made in the admin or by a new data migration.
- Limits are a plain name → number map (`null` = unlimited). Safety Compliance counts clients + business units +
  sites together as `org_units`.
- Licence rules live in `licensing/services.py`: a paid licence counts while active, payment overdue or cancelled
  until period end + LICENSE_GRACE_DAYS; then it expires and the customer is on the product's free tier (issued on
  the spot, renews itself). Data in the apps is never touched by a lapse.
- Private keys only in `SIGNING_KEYS_DIR/<kid>.pem` (mode 600, never committed); only `licensing/keystore.py`
  reads them. Never delete an old key file: certificates signed with it must keep verifying.
- API keys: only the SHA-256 hash is stored (`AppClient`); app keys are limited to their product. The API is server
  to server; keys never go to a browser.
- South African English, plain short wording, as in the apps.

## GitHub

- `SimpologyGroup/pi-licensing-service` (private). Features and data model changes go on a branch with a pull
  request for Johann to merge.
