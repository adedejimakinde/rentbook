from django.apps import AppConfig


class BookingsConfig(AppConfig):
    """Configuration for the bookings app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.bookings"

    def ready(self):
        from apps.bookings import signals  # noqa: F401
