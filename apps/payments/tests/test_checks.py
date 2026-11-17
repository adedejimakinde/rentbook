"""System check for manual payment details in production."""

from apps.payments.checks import MANUAL_PAYMENT_SETTINGS, manual_payment_details_check

ALL_SETTINGS = [name for names in MANUAL_PAYMENT_SETTINGS.values() for name in names]


def fill(settings, value="set"):
    for name in ALL_SETTINGS:
        setattr(settings, name, value)


def test_production_warns_for_each_blank_manual_payment_setting(settings):
    settings.IS_PRODUCTION = True
    fill(settings)
    settings.ZELLE_RECIPIENT = ""
    settings.CASHAPP_TAG = "   "
    settings.BANK_ROUTING_NUMBER = ""

    warnings = manual_payment_details_check(None)

    assert {w.id for w in warnings} == {"payments.W001"}
    messages = " ".join(w.msg for w in warnings)
    assert len(warnings) == 3
    for name in ("ZELLE_RECIPIENT", "CASHAPP_TAG", "BANK_ROUTING_NUMBER"):
        assert name in messages
    assert "by Zelle" in messages and "by Cash App" in messages and "by bank deposit" in messages


def test_production_with_every_detail_set_is_quiet(settings):
    settings.IS_PRODUCTION = True
    fill(settings)

    assert manual_payment_details_check(None) == []


def test_development_never_warns(settings):
    settings.IS_PRODUCTION = False
    fill(settings, "")

    assert manual_payment_details_check(None) == []


def test_the_check_is_registered():
    from django.core import checks

    assert manual_payment_details_check in checks.registry.registry.get_checks()
