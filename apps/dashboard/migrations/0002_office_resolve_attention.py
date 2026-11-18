"""Let Office resolve bookings that need attention, and read the booking log."""

from django.contrib.auth.management import create_permissions
from django.db import migrations

NEW = [("bookings", "resolve_attention"), ("bookings", "view_bookinglogentry")]


def add_permissions(apps, schema_editor):
    for app_config in apps.get_app_configs():
        app_config.models_module = True
        create_permissions(app_config, apps=apps, verbosity=0)
        app_config.models_module = None
    group = apps.get_model("auth", "Group").objects.filter(name="Office").first()
    permission = apps.get_model("auth", "Permission")
    if group is not None:
        for app_label, codename in NEW:
            group.permissions.add(
                permission.objects.get(content_type__app_label=app_label, codename=codename)
            )


def remove_permissions(apps, schema_editor):
    group = apps.get_model("auth", "Group").objects.filter(name="Office").first()
    permission = apps.get_model("auth", "Permission")
    if group is not None:
        for app_label, codename in NEW:
            group.permissions.remove(
                *permission.objects.filter(content_type__app_label=app_label, codename=codename)
            )


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0001_staff_groups"),
        ("bookings", "0014_fill_booking_city"),
    ]

    operations = [migrations.RunPython(add_permissions, remove_permissions)]
