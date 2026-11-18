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


class FakeTwilioMessages:
    """Stands in for Client().messages: records each message; fail_with makes create() raise."""

    def __init__(self):
        self.sent = []
        self.fail_with = None

    def create(self, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(kwargs)
        return type("Message", (), {"sid": f"SM{len(self.sent):032d}"})()

    def to(self, number):
        return [message for message in self.sent if number in message["to"]]


@pytest.fixture
def twilio(settings, monkeypatch):
    """Twilio configured (SMS only) with a fake client. No test talks to Twilio."""
    from apps.notifications.services import sms

    settings.TWILIO_ACCOUNT_SID = "AC" + "0" * 32
    settings.TWILIO_AUTH_TOKEN = "test-token"  # noqa: S105
    settings.TWILIO_FROM_NUMBER = "+14695550000"
    settings.WHATSAPP_ENABLED = False
    settings.TWILIO_WHATSAPP_FROM = ""
    settings.WHATSAPP_TEMPLATES = {}
    settings.OWNER_PHONE = ""
    settings.REVIEW_URL = ""
    settings.BALANCE_DUE_DAYS_BEFORE = 7
    fake = FakeTwilioMessages()
    client = type("Client", (), {"messages": fake})()
    monkeypatch.setattr(sms, "client", lambda: client)
    return fake


class Clock:
    def __init__(self):
        self.now = None

    def set(self, value):
        """Set the time from a naive business-time datetime."""
        from django.utils import timezone

        self.now = timezone.make_aware(value) if timezone.is_naive(value) else value
        return self.now


@pytest.fixture
def clock(monkeypatch):
    """Control timezone.now(). Starts at the real time."""
    from django.utils import timezone

    real_now = timezone.now
    state = Clock()
    state.now = real_now()
    monkeypatch.setattr(timezone, "now", lambda: state.now)
    return state
