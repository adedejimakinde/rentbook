"""Create the Owner, Office and Crew groups with their permissions."""

from django.contrib.auth.management import create_permissions
from django.db import migrations

from apps.dashboard import roles


def make_permissions(apps):
    """Permissions are normally created after migrating; the groups need them now."""
    for app_config in apps.get_app_configs():
        app_config.models_module = True
        create_permissions(app_config, apps=apps, verbosity=0)
        app_config.models_module = None


def find(apps, pairs):
    permission = apps.get_model("auth", "Permission")
    found = []
    for app_label, codename in pairs:
        found.append(
            permission.objects.get(content_type__app_label=app_label, codename=codename)
        )
    return found


def create_groups(apps, schema_editor):
    make_permissions(apps)
    group = apps.get_model("auth", "Group")
    permission = apps.get_model("auth", "Permission")
    group.objects.get_or_create(name=roles.OWNER)[0].permissions.set(permission.objects.all())
    group.objects.get_or_create(name=roles.OFFICE)[0].permissions.set(
        find(apps, roles.OFFICE_PERMISSIONS)
    )
    group.objects.get_or_create(name=roles.CREW)[0].permissions.set(
        find(apps, roles.CREW_PERMISSIONS)
    )


def remove_groups(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(
        name__in=[roles.OWNER, roles.OFFICE, roles.CREW]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("bookings", "0012_crew_times_and_dashboard_permissions"),
        ("catalogue", "0004_item_needs_power"),
        ("core", "0003_crew_and_wind_switches"),
        ("inventory", "0001_stock_adjustment"),
        ("invoices", "0002_refund_reversal"),
        ("notifications", "0002_notification_log_and_sms_opt_out"),
        ("payments", "0006_refund_failure_reason_and_mismatch_flag"),
    ]

    operations = [migrations.RunPython(create_groups, remove_groups)]
