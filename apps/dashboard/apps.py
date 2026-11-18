from django.apps import AppConfig
from django.db.models.signals import post_migrate


def sync_owner_group(sender, **kwargs):
    """Keep the Owner group holding every permission, including ones added later."""
    from django.contrib.auth.models import Group, Permission

    owner = Group.objects.filter(name="Owner").first()
    if owner is not None:
        owner.permissions.add(*Permission.objects.exclude(group=owner))


class DashboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.dashboard"

    def ready(self):
        post_migrate.connect(sync_owner_group, dispatch_uid="dashboard.sync_owner_group")
