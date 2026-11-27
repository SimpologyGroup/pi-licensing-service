# PI - Licensing Service

One licensing service for the PI apps (PI - Safety Compliance Tracker, PI-Cashflow Management and later products).
It keeps each customer's licence per product, signs licence certificates, and answers the apps when they ask
which tier an account is on.

Design: [PI Licensing Service: Design v1](https://claude.ai/artifact/VJAm4jcwHu1XLs9AEoE3hL).

## What is here

- `package/` – **pi-license**, the small Python package both apps install. It verifies certificates (Ed25519
  signatures, dates, product), reads the tier's limits and talks to this service.
- `licensing/` – the Django service: products and tiers (loaded with the agreed tiers), customers, licences,
  certificates, app keys and the JSON API.

## First run (Mac)

Needs Python 3.11 or newer. The Mac's own `python3` is 3.9, so install Homebrew's first: `brew install python@3.12`.

```
cp .env.example .env            # fill in DJANGO_SECRET_KEY and the database
createdb pi_licensing
./start.sh --install                                   # makes .venv with Python 3.11+ and a current pip, then stops
.venv/bin/python manage.py generate_signing_key        # prints the kid: put it in .env as SIGNING_KID
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py create_api_key "Safety app" --product pi-safety
.venv/bin/python manage.py create_api_key "Cash Flow app" --product pi-cashflow
.venv/bin/python manage.py create_api_key "Website"
./start.sh                                             # http://localhost:8002/admin/
```

Folders with spaces in their names are fine. If the clone is in Desktop or Documents and iCloud syncs them, the venv
lives in `.venv.nosync` (iCloud leaves it alone) and `.venv` points to it; `start.sh` sets this up. The `keys/`
folder is synced too, so for the live service keep the clone outside iCloud.

If `manage.py` says `No module named 'pi_license'` or `cannot import name ... (unknown location)`, the venv is
damaged. Make a new one: `rm -rf .venv .venv.nosync && ./start.sh --install`.

Keep a copy of the key file in `keys/` somewhere safe: without it no new certificates can be signed, and with it
anyone can sign them.

## API

All calls except the public keys need `Authorization: Bearer <api key>`. App keys only see their own product.

| Call | Who | What |
| --- | --- | --- |
| `GET /.well-known/pi-license-keys` | anyone | Public keys (JWKS) for checking certificates |
| `GET /v1/accounts/licenses?email=&product=&account=&org_id=` | app backend | Licence to use for a verified email and account (ties it to the account); free Tier 1 is issued when there is none |
| `GET /v1/licenses?email=&product=&account=` | app backend | The email's licences to pick from, each marked free to use, used by this account (`this`) or another (`other`); no certificates |
| `POST /v1/free-licenses` `{email, product, name, org, org_id, account}` | website, app | Free Tier 1 licence at sign-up; with `account`, that app account's own free licence, tied to it (an account with no licence falls back to it) |
| `GET /v1/licenses/<lid>/status` | app backend | Status; `current` is the licence now in use |
| `GET /v1/licenses/<lid>/certificate` | app, website | The `.pilic` file |
| `POST /v1/activations` `{lid, account}` | app backend | Tie a licence to an app account; `409 in_use` when another account has it |

**One licence, one account.** A licence belongs to the first app account (in one app installation, i.e. one app
key) that links or uploads it; its certificate then names that account (`account`) and installation
(`installation`, the middle part of the app key) and the apps refuse it anywhere else. One email can hold several
licences (one per account); each account also gets its own free licence. To move a licence to another account,
open it in the admin and delete the line under "account using this licence".

**Organisations.** Every organisation has an **Org ID** (ORG-00001, ORG-00002 ...), given by this service when it is
first saved and the same for every PI app: an organisation's Cash Flow and Safety licences hang off the same Org ID.
It can never be changed, and organisations are never deleted (only renamed). Customers (emails) belong to an
organisation; a new customer from the API joins the organisation given by `org_id`, or a new one named `org`.
Certificates carry `org` (the name) and `org_id`. In the admin, Organisations lists them; a licence issued by hand
is for a customer picked from the list (add a new customer with the + next to it).

## Tests

```
.venv/bin/python manage.py test licensing
cd package && ../.venv/bin/python -m unittest discover -s tests   # tests the installed package/src/pi_license
```

## Not built yet

Paddle payments (webhooks, checkout links), the website's licence pages, and the changes in both apps
(sign-in lookup, upload screen, enforcing limits).
