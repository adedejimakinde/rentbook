import pytest

from apps.payments.tests.helpers import WEBHOOK_SECRET


@pytest.fixture(autouse=True)
def stripe_settings(settings):
    """Fake Stripe credentials; no test ever talks to Stripe."""
    settings.STRIPE_SECRET_KEY = "sk_test_fake"  # noqa: S105
    settings.STRIPE_WEBHOOK_SECRET = WEBHOOK_SECRET
    settings.DEPOSIT_PERCENT = 50
