"""Production warns when email would go through Resend without an API key."""

import pytest

from apps.notifications.checks import RESEND_BACKEND, resend_key_check


def test_warns_in_production_without_a_resend_key(settings):
    settings.IS_PRODUCTION = True
    settings.EMAIL_BACKEND = RESEND_BACKEND
    settings.RESEND_API_KEY = " "

    [warning] = resend_key_check(None)

    assert warning.id == "notifications.W001"


def test_quiet_with_a_key_or_outside_production(settings):
    settings.EMAIL_BACKEND = RESEND_BACKEND
    settings.IS_PRODUCTION = True
    settings.RESEND_API_KEY = "re_live_key"
    assert resend_key_check(None) == []

    settings.IS_PRODUCTION = False
    settings.RESEND_API_KEY = ""
    assert resend_key_check(None) == []


def twilio_warnings():
    from apps.notifications.checks import twilio_settings_check

    return {warning.id for warning in twilio_settings_check(None)}


@pytest.fixture
def production(settings):
    settings.IS_PRODUCTION = True
    for name in (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
        "TWILIO_WHATSAPP_FROM",
        "OWNER_PHONE",
    ):
        setattr(settings, name, "")
    settings.WHATSAPP_ENABLED = False
    settings.WHATSAPP_TEMPLATES = {}
    return settings


def test_no_twilio_warning_when_unconfigured_or_fully_configured(production):
    assert twilio_warnings() == set()
    production.TWILIO_ACCOUNT_SID = "AC1"
    production.TWILIO_AUTH_TOKEN = "token"  # noqa: S105
    production.TWILIO_FROM_NUMBER = "+14695550000"
    assert twilio_warnings() == set()


@pytest.mark.parametrize(
    "missing", ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"]
)
def test_half_configured_twilio_warns(production, missing):
    production.TWILIO_ACCOUNT_SID = "AC1"
    production.TWILIO_AUTH_TOKEN = "token"  # noqa: S105
    production.TWILIO_FROM_NUMBER = "+14695550000"
    setattr(production, missing, "")
    assert twilio_warnings() == {"notifications.W002"}


def test_whatsapp_enabled_without_sender_or_templates_warns(production):
    production.WHATSAPP_ENABLED = True
    assert twilio_warnings() == {"notifications.W003", "notifications.W004"}


def test_owner_phone_without_twilio_warns(production):
    production.OWNER_PHONE = "+14695550199"
    assert twilio_warnings() == {"notifications.W005"}


def test_no_twilio_warnings_outside_production(production):
    production.IS_PRODUCTION = False
    production.TWILIO_ACCOUNT_SID = "AC1"
    assert twilio_warnings() == set()
