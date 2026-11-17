"""System checks for payment settings."""

from django.conf import settings
from django.core.checks import Warning, register

# Settings shown to customers who pay by hand, grouped by method.
MANUAL_PAYMENT_SETTINGS = {
    "Zelle": ("ZELLE_RECIPIENT", "ZELLE_NAME"),
    "Cash App": ("CASHAPP_TAG",),
    "bank deposit": (
        "BANK_NAME",
        "BANK_ACCOUNT_NAME",
        "BANK_ACCOUNT_NUMBER",
        "BANK_ROUTING_NUMBER",
    ),
}


@register("payments")
def manual_payment_details_check(app_configs, **kwargs):
    """In production, warn about each blank Zelle, Cash App or bank detail.

    Customers who choose that method would otherwise see "Ask us" instead of
    where to send the money.
    """
    if not getattr(settings, "IS_PRODUCTION", False):
        return []
    return [
        Warning(
            f"{name} is blank, so customers paying by {method} are not told where to pay.",
            hint=f"Set {name} in the environment (see .env.example).",
            id="payments.W001",
        )
        for method, names in MANUAL_PAYMENT_SETTINGS.items()
        for name in names
        if not str(getattr(settings, name, "") or "").strip()
    ]
