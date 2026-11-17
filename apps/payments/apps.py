from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    """Configuration for the payments app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.payments"

    def ready(self):
        from apps.payments import checks  # noqa: F401  registers the system checks
