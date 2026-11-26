#!/usr/bin/env bash
# Start PI - Licensing Service on port 8002 (Cash Flow uses 8000, Safety Compliance 8001).
# Also makes the .venv the first time. ./start.sh --install only sets up the .venv and stops.
set -euo pipefail
cd "$(dirname "$0")"

# Python 3.11 or newer (Django 5.2 and pi-license need it). macOS's own python3 is 3.9, so prefer Homebrew's.
pick_python() {
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

if [ ! -e .venv ]; then
    PYTHON=$(pick_python) || {
        echo "Python 3.11 or newer is needed. On a Mac: brew install python@3.12, then run ./start.sh again." >&2
        exit 1
    }
    if [ "$(uname)" = "Darwin" ]; then
        # iCloud syncs Desktop and Documents and makes "file 2" copies inside a venv, which breaks imports.
        # It leaves folders ending in .nosync alone, so the venv lives there and .venv points to it.
        "$PYTHON" -m venv .venv.nosync
        ln -sfn .venv.nosync .venv
    else
        "$PYTHON" -m venv .venv
    fi
fi

if ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    echo ".venv uses Python older than 3.11 or is broken. Remove it (rm -rf .venv .venv.nosync) and run ./start.sh again." >&2
    exit 1
fi

.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
.venv/bin/python -c 'from pi_license import build_payload' 2>/dev/null || {
    echo "pi_license does not import from .venv. Remove it (rm -rf .venv .venv.nosync) and run ./start.sh again." >&2
    exit 1
}
if [ "${1:-}" = "--install" ]; then
    echo "Python environment ready: .venv/bin/python manage.py ... works now."
    exit 0
fi

.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py collectstatic --noinput -v0
exec .venv/bin/waitress-serve --listen=127.0.0.1:${PORT:-8002} licensing_site.wsgi:application
