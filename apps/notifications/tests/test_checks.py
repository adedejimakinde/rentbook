"""Production warns when email would go through Resend without an API key."""

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
