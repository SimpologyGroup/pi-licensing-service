from django.db import migrations


def load(apps, schema_editor):
    from licensing import catalogue

    catalogue.load(apps.get_model("licensing", "Product"), apps.get_model("licensing", "Tier"))


class Migration(migrations.Migration):
    dependencies = [("licensing", "0001_initial")]

    operations = [migrations.RunPython(load, migrations.RunPython.noop)]
