"""The owner's switches (SiteSettings), cached, and what is actually available.

A channel or payment method is active only when its technical settings are
configured AND its switch is on. Every view and service reads the switches
through this module.
"""

from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

CACHE_KEY = "core:site_settings"
CACHE_SECONDS = 300


def get():
    """The single SiteSettings row, from the cache when possible (created on first use)."""
    from apps.core.models import SiteSettings

    row = cache.get(CACHE_KEY)
    if row is None:
        row, _ = SiteSettings.objects.get_or_create(pk=1)
        cache.set(CACHE_KEY, row, CACHE_SECONDS)
    return row


def forget():
    """Drop the cached row so the next read sees the database."""
    cache.delete(CACHE_KEY)


def _missing(*names):
    return [name for name in names if not str(getattr(settings, name, "") or "").strip()]


@dataclass(frozen=True)
class Availability:
    label: str
    switch: str
    switched_on: bool
    missing: tuple

    @property
    def configured(self):
        return not self.missing

    @property
    def active(self):
        return self.configured and self.switched_on


def _sms_missing():
    return _missing("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")


def _whatsapp_missing():
    missing = _missing("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM")
    if not getattr(settings, "WHATSAPP_ENABLED", False):
        missing.insert(0, "WHATSAPP_ENABLED")
    return missing


PAYMENT_SETTINGS = {
    "stripe": ("card_ach_enabled", ("STRIPE_SECRET_KEY",)),
    "zelle": ("zelle_enabled", ("ZELLE_RECIPIENT", "ZELLE_NAME")),
    "cashapp": ("cashapp_enabled", ("CASHAPP_TAG",)),
    "bank_deposit": (
        "bank_deposit_enabled",
        ("BANK_NAME", "BANK_ACCOUNT_NAME", "BANK_ACCOUNT_NUMBER", "BANK_ROUTING_NUMBER"),
    ),
}


def payment_method(choice, row=None):
    """Availability of a payment choice ("stripe", "zelle", "cashapp", "bank_deposit")."""
    from apps.bookings.models import PaymentChoice

    row = row or get()
    switch, names = PAYMENT_SETTINGS[choice]
    return Availability(
        PaymentChoice(choice).label, switch, getattr(row, switch), tuple(_missing(*names))
    )


def payment_method_active(choice):
    return payment_method(choice).active


def active_payment_choices():
    """The payment choices new online bookings may use, in display order."""
    return [choice for choice in PAYMENT_SETTINGS if payment_method_active(choice)]


def sms(row=None):
    row = row or get()
    return Availability("SMS", "sms_enabled", row.sms_enabled, tuple(_sms_missing()))


def whatsapp(row=None):
    row = row or get()
    return Availability(
        "WhatsApp", "whatsapp_enabled", row.whatsapp_enabled, tuple(_whatsapp_missing())
    )


def owner_alerts(row=None):
    row = row or get()
    return Availability(
        "Owner text alerts",
        "owner_alerts_enabled",
        row.owner_alerts_enabled,
        tuple(_missing("OWNER_PHONE")),
    )


def all_availability(row=None):
    """Every channel and payment method with whether it is configured and switched on."""
    row = row or get()
    return [
        *(payment_method(choice, row) for choice in PAYMENT_SETTINGS),
        sms(row),
        whatsapp(row),
        owner_alerts(row),
    ]


def online_booking_enabled():
    return get().online_booking_enabled


def deposit_allowed():
    return get().allow_deposit


def promo_codes_enabled():
    return get().promo_codes_enabled


def tax_exempt_requests_enabled():
    return get().tax_exempt_requests_enabled


def reminder_enabled(switch):
    return getattr(get(), switch)
