#!/usr/bin/env bash
# Start PI - Licensing Service on port 8002 (Cash Flow uses 8000, Safety Compliance 8001).
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py collectstatic --noinput -v0
exec .venv/bin/waitress-serve --listen=127.0.0.1:${PORT:-8002} licensing_site.wsgi:application
