"""Zelle, Cash App and bank deposit details, from settings, for pages, emails and invoices."""

from django.conf import settings

from apps.bookings.models import MANUAL_PAYMENT_CHOICES, PaymentChoice


def manual_details():
    """Each manual method's (label, value) pairs, keyed by PaymentChoice."""
    return {
        PaymentChoice.ZELLE: [
            ("Send to", settings.ZELLE_RECIPIENT),
            ("Name shown", settings.ZELLE_NAME),
        ],
        PaymentChoice.CASHAPP: [("Cash App tag", settings.CASHAPP_TAG)],
        PaymentChoice.BANK_DEPOSIT: [
            ("Bank", settings.BANK_NAME),
            ("Account name", settings.BANK_ACCOUNT_NAME),
            ("Account number", settings.BANK_ACCOUNT_NUMBER),
            ("Routing number", settings.BANK_ROUTING_NUMBER),
        ],
    }


def payment_memo(booking):
    """What the customer puts in the payment note so staff can match the money."""
    return f"Booking {booking.pk}"


def instructions():
    """Every manual method with its label and details, for invoices."""
    details = manual_details()
    return [
        {"method": PaymentChoice(choice).label, "details": details[choice]}
        for choice in MANUAL_PAYMENT_CHOICES
    ]
