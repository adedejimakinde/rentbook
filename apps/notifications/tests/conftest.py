"""Fixtures for invoice and email tests: the fake Stripe from the payment tests, and pricing."""

from decimal import Decimal

import pytest

from apps.payments.tests.conftest import intents, refunds, sessions, stripe_settings  # noqa: F401


@pytest.fixture(autouse=True)
def pricing(settings):
    """No tax, 50% deposit, known business and payment details."""
    settings.SALES_TAX_RATE = Decimal("0")
    settings.TAX_DELIVERY = False
    settings.TAX_SETUP = False
    settings.DEPOSIT_PERCENT = 50
    settings.SITE_URL = "https://rentals.example"
    settings.BUSINESS_NAME = "Atglace Events & Rentals LLC"
    settings.BUSINESS_EMAIL = "owner@rentals.example"
    settings.DEFAULT_FROM_EMAIL = "bookings@rentals.example"
    settings.ZELLE_RECIPIENT = "pay@rentals.example"
    settings.ZELLE_NAME = "Atglace Events"
    settings.CASHAPP_TAG = "$AtglaceEvents"
    settings.BANK_NAME = "First Bank"
    settings.BANK_ACCOUNT_NAME = "Atglace Events LLC"
    settings.BANK_ACCOUNT_NUMBER = "123456789"
    settings.BANK_ROUTING_NUMBER = "111000025"
