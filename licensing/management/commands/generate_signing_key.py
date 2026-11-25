import os
from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from pi_license import keys


class Command(BaseCommand):
    help = "Make a new Ed25519 signing key in SIGNING_KEYS_DIR and print its kid (put it in .env as SIGNING_KID)."

    def add_arguments(self, parser):
        parser.add_argument("--kid", help="Key id; default pi-<year>-<month>.")

    def handle(self, *args, **options):
        kid = options["kid"] or f"pi-{date.today():%Y-%m}"
        folder = settings.LICENSE_SIGNING_KEYS_DIR
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{kid}.pem"
        if path.exists():
            raise CommandError(f"{path} already exists; keys are never overwritten.")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(keys.private_pem(keys.generate()))
        self.stdout.write(f"Saved {path} (readable by this user only).")
        self.stdout.write(f"Set SIGNING_KID={kid} in .env. Keep a copy of the key file in a safe place.")
