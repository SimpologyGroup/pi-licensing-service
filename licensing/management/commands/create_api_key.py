from django.core.management.base import BaseCommand, CommandError

from licensing.models import AppClient, Product


class Command(BaseCommand):
    help = (
        "Make an API key for an app's backend (--product pi-safety) or the website (no --product). "
        "The key is shown once; only its hash is kept."
    )

    def add_arguments(self, parser):
        parser.add_argument("name")
        parser.add_argument("--product", help="Product code the key is limited to.")

    def handle(self, *args, name, product=None, **options):
        product_obj = None
        if product:
            product_obj = Product.objects.filter(code=product).first()
            if product_obj is None:
                raise CommandError(f"Unknown product {product!r}.")
        _, key = AppClient.create_with_key(name, product_obj)
        self.stdout.write(f"API key for {name}: {key}")
        self.stdout.write("Copy it now into that app's .env as LICENSING_API_KEY; it cannot be shown again.")
