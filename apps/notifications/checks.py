"""System checks for email settings."""

from django.conf import settings
from django.core.checks import Warning, register

RESEND_BACKEND = "anymail.backends.resend.EmailBackend"


@register("notifications")
def resend_key_check(app_configs, **kwargs):
    """In production, warn when email goes through Resend with no API key."""
    if not getattr(settings, "IS_PRODUCTION", False) or settings.EMAIL_BACKEND != RESEND_BACKEND:
        return []
    if str(settings.RESEND_API_KEY or "").strip():
        return []
    return [
        Warning(
            "RESEND_API_KEY is blank, so no customer or owner email can be sent.",
            hint="Set RESEND_API_KEY in the environment (see .env.example).",
            id="notifications.W001",
        )
    ]
