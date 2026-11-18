"""Booking tests run with every payment method configured, as a live site would be."""

import pytest


@pytest.fixture(autouse=True)
def payment_methods_configured(settings):
    settings.STRIPE_SECRET_KEY = "sk_test_fake"  # noqa: S105
    settings.ZELLE_RECIPIENT = settings.ZELLE_RECIPIENT or "pay@rentals.example"
    settings.ZELLE_NAME = settings.ZELLE_NAME or "Atglace Events"
    settings.CASHAPP_TAG = settings.CASHAPP_TAG or "$AtglaceEvents"
    settings.BANK_NAME = settings.BANK_NAME or "First Bank"
    settings.BANK_ACCOUNT_NAME = settings.BANK_ACCOUNT_NAME or "Atglace Events LLC"
    settings.BANK_ACCOUNT_NUMBER = settings.BANK_ACCOUNT_NUMBER or "123456789"
    settings.BANK_ROUTING_NUMBER = settings.BANK_ROUTING_NUMBER or "111000025"
