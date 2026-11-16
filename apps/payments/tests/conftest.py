from unittest import mock

import pytest

from apps.payments.services import stripe as stripe_service
from apps.payments.tests.helpers import (
    WEBHOOK_SECRET,
    FakeStripePaymentIntents,
    FakeStripeRefunds,
    FakeStripeSessions,
)


@pytest.fixture(autouse=True)
def stripe_settings(settings):
    """Fake Stripe credentials; no test ever talks to Stripe."""
    settings.STRIPE_SECRET_KEY = "sk_test_fake"  # noqa: S105
    settings.STRIPE_WEBHOOK_SECRET = WEBHOOK_SECRET
    settings.DEPOSIT_PERCENT = 50


@pytest.fixture
def refunds():
    """Replace stripe.Refund with an in-memory fake."""
    fake = FakeStripeRefunds()
    with mock.patch.object(stripe_service.stripe, "Refund", fake):
        yield fake


@pytest.fixture(autouse=True)
def sessions():
    """Replace stripe.checkout.Session with an in-memory fake for every test."""
    fake = FakeStripeSessions()
    with mock.patch.object(stripe_service.stripe.checkout, "Session", fake):
        yield fake


@pytest.fixture
def intents():
    """Replace stripe.PaymentIntent with an in-memory fake."""
    fake = FakeStripePaymentIntents()
    with mock.patch.object(stripe_service.stripe, "PaymentIntent", fake, create=True):
        yield fake
