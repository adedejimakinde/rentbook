from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Configuration for the core app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"

    def ready(self):
        from apps.core import signals  # noqa: F401  clears the site settings cache on save
