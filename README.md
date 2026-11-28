# PI - Licensing Service

One licensing service for the PI apps (Safety Compliance Insight, PI-Cashflow Management and later products).
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
| `GET /v1/accounts/licenses?email=&product=&account=&org_id=` | app backend | Licence to use for a confirmed email and account (ties it to the account); free Tier 1 is issued when there is none |
| `GET /v1/licenses?email=&product=&account=` (or `org_id=` instead of `email=`) | app backend | The email's licences (once confirmed with a code) or the linked organisation's licences, to pick from, each marked free to use, used by this account (`this`) or another (`other`); no certificates |
| `GET /v1/organisations/<org_id>?account=` | app backend | Org ID and name of the organisation the account is linked to (404 for any other) |
| `POST /v1/email-codes` `{email, account, account_name, requested_by}` | app backend | Email a six-digit code to a licence email (15 minutes); same answer whether or not it holds licences; `429 too_many` after 5 an hour |
| `POST /v1/email-codes/check` `{email, account, code}` | app backend | Right code: the account may list and use that email's licences for 24 hours; `400 bad_code`, `429 too_many` after 5 wrong tries |
| `POST /v1/org-links` `{org_id, account}` | app backend | Link the account to an organisation once it has confirmed the email of someone in it; `403 not_proven` |
| `POST /v1/free-licenses` `{email, product, name, org, org_id, account}` | website, app | Free Tier 1 licence at sign-up; with `account`, that app account's own free licence, tied to it (an account with no licence falls back to it) |
| `GET /v1/licenses/<lid>/status` | app backend | Status; `current` is the licence now in use |
| `GET /v1/licenses/<lid>/certificate` | app, website | The `.pilic` file |
| `POST /v1/activations` `{lid, account, org_id, certificate}` | app backend | Tie a licence to an app account (`403 not_proven` without a confirmed email, a linked organisation or the certificate); `409 in_use` when another account has it, `409 other_org` when it belongs to another organisation than `org_id` |

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

## Store and Paddle payments

`/store/` lists every active product's tiers with their prices and limits. Paid tiers get a **Subscribe** button
once the store has a Paddle client-side token and the tier has a Paddle price id; otherwise they show "Coming Soon".
The buyer types email, name and organisation (or an Org ID), then pays in Paddle's checkout. Paddle tells this
service at `/webhooks/paddle`, which makes the licence (the customer joins the given organisation or a new one) and
emails the licence id with the steps to use it (app → Subscription → Find Licences → type the emailed code → Use
This Licence).
Upgrades, renewals, overdue payments and cancellations update the same licence; a cancelled one runs to the end of
the paid year (plus the grace days), then the account is back on the free tier.

To switch it on (sandbox first, at sandbox-vendors.paddle.com):

1. In Paddle, make a product per app and a yearly USD price per paid tier. Copy each price id (`pri_...`) into the
   tier's **Provider price id** in the admin (Products → the product → its tiers).
2. Developer Tools → Authentication: make a **client-side token** → `PADDLE_CLIENT_TOKEN` in `.env`.
3. Developer Tools → Notifications: new destination `https://<this service>/webhooks/paddle` with the
   `subscription.*` and `customer.created`/`customer.updated` events; copy its secret key →
   `PADDLE_WEBHOOK_SECRET`.
4. Checkout → Website approval: add the store's domain; set the default payment link to `https://<this service>/store/`.
5. Fill in the email settings in `.env` so the licence emails go out (without `EMAIL_HOST` they are printed).
6. Restart. For production set `PADDLE_ENV=production` and use the live token, secret and price ids.

Every notification is kept under Admin → Webhook events. One marked "Waiting" or "Failed" can be applied again with
the **Process again** action.

## Not built yet

A "Buy or upgrade" link from the apps' Subscription page to the store.

## Proof before licences are shown

An email can be guessed and Org IDs count up, so neither is proof on its own (`licensing/proof.py`). An app
administrator types the email the licences are held under; the service emails a six-digit code to it (it names the
account and who asked). Only a hash is kept; it works for 15 minutes and 5 tries, and a new code replaces the old
one. With the right code that account may list and use the email's licences for 24 hours (`EmailProof`). An account
is linked to an organisation (`OrgLink`) only after confirming the email of someone in it, or by using one of its
licences; only then are the organisation's licences listed. Uploading a licence's certificate is proof too. The
website's key needs no proof. Settings: `LICENSE_CODE_MINUTES`, `LICENSE_CODE_TRIES`, `LICENSE_CODES_PER_HOUR`,
`LICENSE_EMAIL_PROOF_HOURS`; the codes go out with the email settings in `.env`.
