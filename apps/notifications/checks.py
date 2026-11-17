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


TWILIO_CORE = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN")


def _blank(name):
    return not str(getattr(settings, name, "") or "").strip()


@register("notifications")
def twilio_settings_check(app_configs, **kwargs):
    """In production, warn when Twilio is half-configured, so texts silently never send."""
    if not getattr(settings, "IS_PRODUCTION", False):
        return []
    names = (*TWILIO_CORE, "TWILIO_FROM_NUMBER")
    set_names = [name for name in names if not _blank(name)]
    warnings = []
    if set_names and len(set_names) < len(names):
        missing = ", ".join(name for name in names if _blank(name))
        warnings.append(
            Warning(
                f"Twilio is partly configured ({missing} blank), so no SMS can be sent.",
                hint="Set all of TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER, "
                "or leave all three blank.",
                id="notifications.W002",
            )
        )
    if getattr(settings, "WHATSAPP_ENABLED", False):
        missing = [name for name in (*TWILIO_CORE, "TWILIO_WHATSAPP_FROM") if _blank(name)]
        if missing:
            warnings.append(
                Warning(
                    f"WHATSAPP_ENABLED is on but {', '.join(missing)} is blank, so no WhatsApp "
                    "message can be sent.",
                    hint="Set the missing values or turn WHATSAPP_ENABLED off.",
                    id="notifications.W003",
                )
            )
        if not settings.WHATSAPP_TEMPLATES:
            warnings.append(
                Warning(
                    "WHATSAPP_ENABLED is on but WHATSAPP_TEMPLATES is empty, so every WhatsApp "
                    "message is skipped.",
                    hint="Add approved template SIDs as kind=HX...,kind=HX...",
                    id="notifications.W004",
                )
            )
    if not _blank("OWNER_PHONE") and len(set_names) < len(names):
        warnings.append(
            Warning(
                "OWNER_PHONE is set but Twilio is not fully configured, so owner alerts are "
                "not texted.",
                hint="Configure Twilio, or clear OWNER_PHONE.",
                id="notifications.W005",
            )
        )
    return warnings
