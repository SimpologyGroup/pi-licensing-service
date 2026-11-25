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

```
cp .env.example .env            # fill in DJANGO_SECRET_KEY and the database
createdb pi_licensing
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py generate_signing_key        # prints the kid: put it in .env as SIGNING_KID
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py create_api_key "Safety app" --product pi-safety
.venv/bin/python manage.py create_api_key "Cash Flow app" --product pi-cashflow
.venv/bin/python manage.py create_api_key "Website"
./start.sh                                             # http://localhost:8002/admin/
```

Keep a copy of the key file in `keys/` somewhere safe: without it no new certificates can be signed, and with it
anyone can sign them.

## API

All calls except the public keys need `Authorization: Bearer <api key>`. App keys only see their own product.

| Call | Who | What |
| --- | --- | --- |
| `GET /.well-known/pi-license-keys` | anyone | Public keys (JWKS) for checking certificates |
| `GET /v1/accounts/licenses?email=&product=&account=` | app backend | Licence in use for a verified email; free Tier 1 is issued when there is none |
| `POST /v1/free-licenses` `{email, product, name, org}` | website | Free Tier 1 licence at sign-up |
| `GET /v1/licenses/<lid>/status` | app backend | Status; `current` is the licence now in use |
| `GET /v1/licenses/<lid>/certificate` | app, website | The `.pilic` file |
| `POST /v1/activations` `{lid, account}` | app backend | Tie an uploaded licence to an app account |

## Tests

```
.venv/bin/python manage.py test licensing
cd package && ../.venv/bin/python -m unittest discover -s tests
```

## Not built yet

Paddle payments (webhooks, checkout links), the website's licence pages, and the changes in both apps
(sign-in lookup, upload screen, enforcing limits).
