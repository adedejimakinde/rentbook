"""The owner's switches in the cart and booking flow, and SMS consent at the contact step."""

from decimal import Decimal
from itertools import chain, repeat
from unittest import mock

import pytest
from django.utils import timezone

from apps.bookings.models import Booking, PaymentChoice, PromoCode
from apps.bookings.services import booking_flow, links
from apps.bookings.services import quote as quote_service
from apps.bookings.tests.factories import ItemVariantFactory
from apps.bookings.tests.flow_helpers import (
    add,
    catalogue,
    contact_data,
    fill_steps,
    ready_to_review,
    review_data,
    set_details,
    step_url,
)
from apps.bookings.tests.test_admin import ADD_URL, booking_form
from apps.core import site_settings
from apps.notifications.consent import SMS_CONSENT_TEXT, SMS_CONSENT_VERSION
from apps.payments.services import stripe as stripe_service
from apps.payments.tests.helpers import FakeStripeSessions

pytestmark = pytest.mark.django_db
REVIEW = step_url("review")
CONTACT = step_url("contact")
PAUSED = "Online booking is paused"


@pytest.fixture
def items():
    return catalogue()


@pytest.fixture(autouse=True)
def sessions():
    fake = FakeStripeSessions()
    with mock.patch.object(stripe_service.stripe.checkout, "Session", fake):
        yield fake


@pytest.fixture
def twilio_configured(settings):
    settings.TWILIO_ACCOUNT_SID = "AC" + "0" * 32
    settings.TWILIO_AUTH_TOKEN = "token"  # noqa: S105
    settings.TWILIO_FROM_NUMBER = "+14695550000"


def switch(**values):
    row = site_settings.get()
    for name, value in values.items():
        setattr(row, name, value)
    row.save()


def page(response):
    return response.content.decode()


# Online booking.


def test_paused_booking_shows_the_whatsapp_message_everywhere(client, items, event_date):
    ready_to_review(client, event_date, items)
    switch(online_booking_enabled=False)
    for url in ("/cart/", step_url("event"), CONTACT, REVIEW):
        response = client.get(url)
        assert response.status_code == 200
        assert PAUSED in page(response) and "wa.me/" in page(response)
    response = add(client, "variant", items["chair"].pk)
    assert PAUSED in page(response)
    response = client.post(REVIEW, review_data())
    assert PAUSED in page(response)
    assert not Booking.objects.exists()


def test_paused_htmx_requests_get_the_small_partial(client, items):
    switch(online_booking_enabled=False)
    response = add(client, "variant", items["chair"].pk, htmx=True)
    assert "<html" not in page(response) and PAUSED in page(response)


def test_paused_booking_service_refuses(items, event_date):
    switch(online_booking_enabled=False)
    with pytest.raises(booking_flow.NotAllowed):
        booking_flow.check_allowed("deposit", "stripe")


def test_admin_bookings_still_work_while_paused(admin_client):
    switch(online_booking_enabled=False)
    variant = ItemVariantFactory(stock_quantity=3)
    response = admin_client.post(
        ADD_URL, booking_form("held", [{"variant": variant, "quantity": 1}])
    )
    assert response.status_code == 302
    assert Booking.objects.get().status == Booking.Status.HELD


def test_booking_works_again_when_switched_back_on(client, items, event_date):
    switch(online_booking_enabled=False)
    switch(online_booking_enabled=True)
    ready_to_review(client, event_date, items)
    assert client.post(REVIEW, review_data()).status_code == 303


# Payment methods.

SWITCHES = {
    "stripe": "card_ach_enabled",
    "zelle": "zelle_enabled",
    "cashapp": "cashapp_enabled",
    "bank_deposit": "bank_deposit_enabled",
}


def offered(response):
    return {choice for choice in SWITCHES if f'value="{choice}"' in page(response)}


@pytest.mark.parametrize("choice", list(SWITCHES))
def test_a_switched_off_method_is_not_offered_or_accepted(client, items, event_date, choice):
    switch(**{SWITCHES[choice]: False})
    ready_to_review(client, event_date, items)
    assert offered(client.get(REVIEW)) == set(SWITCHES) - {choice}
    response = client.post(REVIEW, review_data(choice=choice))
    assert response.status_code == 422
    assert not Booking.objects.exists()


@pytest.mark.parametrize(
    ("choice", "setting"),
    [
        ("stripe", "STRIPE_SECRET_KEY"),
        ("zelle", "ZELLE_RECIPIENT"),
        ("cashapp", "CASHAPP_TAG"),
        ("bank_deposit", "BANK_ROUTING_NUMBER"),
    ],
)
def test_an_unconfigured_method_is_not_offered_even_when_switched_on(
    client, items, event_date, settings, choice, setting
):
    setattr(settings, setting, "")
    ready_to_review(client, event_date, items)
    assert offered(client.get(REVIEW)) == set(SWITCHES) - {choice}
    availability = site_settings.payment_method(choice)
    assert availability.switched_on and not availability.configured
    assert setting in availability.missing


def test_no_method_available_pauses_checkout(client, items, event_date):
    switch(**dict.fromkeys(SWITCHES.values(), False))
    ready_to_review(client, event_date, items)
    assert PAUSED in page(client.get(REVIEW))


def test_a_method_switched_off_after_review_loads_is_refused(client, items, event_date):
    ready_to_review(client, event_date, items)
    client.get(REVIEW)
    with mock.patch.object(
        site_settings,
        "active_payment_choices",
        side_effect=chain([["zelle"], ["zelle"]], repeat([])),
    ):
        response = client.post(REVIEW, review_data(choice="zelle"))
    assert response.status_code == 409
    assert "not available" in page(response)
    assert not Booking.objects.exists()


def test_an_existing_booking_keeps_its_chosen_method(client, items, event_date):
    ready_to_review(client, event_date, items)
    client.post(REVIEW, review_data(choice="zelle"))
    booking = Booking.objects.get()
    switch(zelle_enabled=False)
    response = client.get(f"/payments/manual/{links.make_token(booking, links.MANUAL)}/")
    assert response.status_code == 200
    assert "pay@rentals.example" in page(response)


# Deposits.


def test_deposit_off_means_full_payment_only(client, items, event_date, sessions):
    switch(allow_deposit=False)
    ready_to_review(client, event_date, items)
    response = client.get(REVIEW)
    assert 'value="deposit"' not in page(response) and 'value="full"' in page(response)
    assert client.post(REVIEW, review_data(plan="deposit")).status_code == 422
    assert not Booking.objects.exists()
    assert client.post(REVIEW, review_data(plan="full")).status_code == 303
    assert Booking.objects.get().payment_plan == "full"


def test_deposit_off_is_enforced_by_the_service():
    switch(allow_deposit=False)
    with pytest.raises(booking_flow.NotAllowed):
        booking_flow.check_allowed("deposit", "stripe")
    booking_flow.check_allowed("full", "stripe")


# Promo codes.


def test_promo_codes_off_ignores_codes_and_hides_the_box(client, items, event_date):
    PromoCode.objects.create(code="SAVE10", percent_off=Decimal("10"))
    add(client, "variant", items["chair"].pk, 10)
    set_details(client, event_date, promo="SAVE10")
    assert "SAVE10" in page(client.get("/cart/"))
    switch(promo_codes_enabled=False)
    response = client.get("/cart/")
    assert 'name="promo_code"' not in page(response)
    assert "Discount" not in page(response)


def test_promo_codes_off_in_the_quote_service(items, event_date):
    PromoCode.objects.create(code="SAVE10", percent_off=Decimal("10"))
    lines = [quote_service.LineRequest(quantity=10, variant_id=items["chair"].pk)]
    assert quote_service.build_quote(lines, event_date, "75001", "SAVE10").discount > 0
    switch(promo_codes_enabled=False)
    quote = quote_service.build_quote(lines, event_date, "75001", "SAVE10")
    assert quote.discount == 0 and quote.promo is None and not quote.errors


# Tax-exempt requests.


def test_tax_exempt_requests_off_hides_and_ignores_the_fields(client, items, event_date):
    switch(tax_exempt_requests_enabled=False)
    add(client, "variant", items["chair"].pk, 10)
    set_details(client, event_date)
    fill_steps(
        client,
        event_date,
        contact=contact_data(tax_exempt="on", org_name="First Church", cert_number="TX-1"),
    )
    assert 'name="tax_exempt"' not in page(client.get(CONTACT))
    client.post(REVIEW, review_data())
    booking = Booking.objects.get()
    assert not booking.tax_exempt_requested and booking.tax_exempt_org_name == ""


# SMS consent at the contact step.


def test_sms_box_is_unticked_with_the_exact_wording(client, items, event_date, twilio_configured):
    add(client, "variant", items["chair"].pk, 1)
    set_details(client, event_date)
    fill_steps(client, event_date)
    html = page(client.get(CONTACT))
    assert SMS_CONSENT_TEXT in html
    assert (
        "Text me booking updates and reminders. Message and data rates may apply. "
        "Reply STOP to opt out."
    ) == SMS_CONSENT_TEXT
    box = html[html.index('name="sms_consent"') :].split(">")[0]
    assert "checked" not in box


def test_sms_consent_is_stored_with_version_and_time(client, items, event_date, twilio_configured):
    before = timezone.now()
    add(client, "variant", items["chair"].pk, 10)
    set_details(client, event_date)
    fill_steps(client, event_date, contact=contact_data(sms_consent="on"))
    client.post(REVIEW, review_data())
    booking = Booking.objects.get()
    assert booking.sms_consent
    assert booking.sms_consent_version == SMS_CONSENT_VERSION
    assert before <= booking.sms_consent_at <= timezone.now()


def test_no_consent_unless_ticked(client, items, event_date, twilio_configured):
    ready_to_review(client, event_date, items)
    client.post(REVIEW, review_data())
    booking = Booking.objects.get()
    assert not booking.sms_consent
    assert booking.sms_consent_version == "" and booking.sms_consent_at is None


def test_no_sms_box_when_texts_are_off(client, items, event_date):
    add(client, "variant", items["chair"].pk, 10)
    set_details(client, event_date)
    fill_steps(client, event_date, contact=contact_data(sms_consent="on"))
    assert 'name="sms_consent"' not in page(client.get(CONTACT))
    client.post(REVIEW, review_data())
    assert not Booking.objects.get().sms_consent


def test_switches_are_cached_and_refreshed_on_save(django_assert_num_queries):
    site_settings.get()
    with django_assert_num_queries(0):
        site_settings.get()
    switch(zelle_enabled=False)
    assert site_settings.get().zelle_enabled is False


def test_payment_choice_labels_cover_every_switch():
    assert set(site_settings.PAYMENT_SETTINGS) == set(PaymentChoice.values)
