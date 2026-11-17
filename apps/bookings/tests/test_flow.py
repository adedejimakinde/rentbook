"""Booking flow: step validation, terms, and turning the cart into a booking and a payment."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
import stripe
from django.urls import reverse
from django.utils import dateformat, timezone

from apps.bookings.models import Booking
from apps.bookings.services import booking_flow
from apps.bookings.services import cart as cart_service
from apps.bookings.services import quote as quote_service
from apps.bookings.tests.flow_helpers import (
    ZIP,
    add,
    catalogue,
    contact_data,
    delivery_data,
    event_data,
    fill_steps,
    ready_to_review,
    review_data,
    set_details,
    step_url,
)
from apps.payments.models import Payment
from apps.payments.services import stripe as stripe_service
from apps.payments.tests.helpers import FakeStripeSessions

pytestmark = pytest.mark.django_db

REVIEW = step_url("review")


@pytest.fixture
def items():
    return catalogue()


@pytest.fixture
def sessions(settings):
    settings.STRIPE_SECRET_KEY = "sk_test_fake"  # noqa: S105
    fake = FakeStripeSessions()
    with mock.patch.object(stripe_service.stripe.checkout, "Session", fake):
        yield fake


@pytest.fixture
def reviewing(client, items, event_date):
    ready_to_review(client, event_date, items)
    return client


def errors_in(response):
    return response.content.decode()


# Guards


def test_flow_needs_a_cart(client, items):
    response = client.get(step_url("event"))

    assert response.status_code == 302 and response.url == reverse("bookings:cart")


def test_later_steps_send_you_back_to_the_first_one_not_done(client, items, event_date):
    add(client, "variant", items["chair"].pk, 2)

    assert client.get(REVIEW).url == step_url("event")
    client.post(step_url("event"), event_data(event_date))
    assert client.get(step_url("contact")).url == step_url("delivery")


def test_unknown_step_is_404(client, items):
    add(client, "variant", items["chair"].pk, 2)

    assert client.get(step_url("payment")).status_code == 404


def test_each_step_shows_a_progress_bar(client, items, event_date):
    add(client, "variant", items["chair"].pk, 2)

    html = client.get(step_url("event")).content.decode()

    assert 'role="progressbar"' in html and "Step 1 of 4: Event details" in html


# Step 1: event


def test_event_step_requires_every_field(client, items):
    add(client, "variant", items["chair"].pk, 2)

    response = client.post(step_url("event"), {})

    html = response.content.decode()
    assert response.status_code == 200
    assert html.count("This field is required.") == 4
    assert booking_flow.EVENT not in client.session.get(booking_flow.FLOW_KEY, {})


def test_event_step_refuses_setup_after_the_start(client, items, event_date):
    add(client, "variant", items["chair"].pk, 2)

    response = client.post(step_url("event"), event_data(event_date, setup_by_time="15:00"))

    assert "Setup must be finished by the event start time." in response.content.decode()


def test_event_step_refuses_a_date_inside_the_notice_period(client, items):
    add(client, "variant", items["chair"].pk, 2)
    tomorrow = timezone.localdate() + timedelta(days=1)

    response = client.post(step_url("event"), event_data(tomorrow))

    assert "hours&#x27; notice" in response.content.decode()


def test_event_step_saves_and_moves_on_and_sets_the_quote_date(client, items, event_date):
    add(client, "variant", items["chair"].pk, 2)

    response = client.post(step_url("event"), event_data(event_date))

    assert response.url == step_url("delivery")
    assert client.session["event_date"] == event_date.isoformat()


# Step 2: delivery


def done_event(client, items, event_date):
    add(client, "variant", items["chair"].pk, 2)
    client.post(step_url("event"), event_data(event_date))


def test_delivery_step_refuses_a_zip_outside_every_active_zone(client, items, event_date):
    done_event(client, items, event_date)
    items["zone"].is_active = False
    items["zone"].save()

    for zip_code in ("75001", "90210"):
        response = client.post(step_url("delivery"), delivery_data(zip_code=zip_code))
        assert f"We do not deliver to ZIP code {zip_code} yet." in response.content.decode()
    assert booking_flow.DELIVERY not in client.session[booking_flow.FLOW_KEY]


def test_delivery_step_checks_zip_state_and_surface(client, items, event_date):
    done_event(client, items, event_date)

    response = client.post(
        step_url("delivery"), delivery_data(zip_code="7500", state="T1", surface="sand")
    )

    html = response.content.decode()
    assert "Enter a 5-digit ZIP code." in html
    assert "Enter the two-letter state, for example TX." in html
    assert "Select a valid choice." in html


def test_delivery_step_saves_and_sets_the_quote_zip(client, items, event_date):
    done_event(client, items, event_date)

    response = client.post(step_url("delivery"), delivery_data(zip_code="75001-1234"))

    assert response.url == step_url("contact")
    saved = client.session[booking_flow.FLOW_KEY][booking_flow.DELIVERY]
    assert saved["zip_code"] == ZIP and saved["state"] == "TX"
    assert client.session["event_zip"] == ZIP


# Step 3: contact


def done_delivery(client, items, event_date):
    done_event(client, items, event_date)
    client.post(step_url("delivery"), delivery_data())


def test_contact_step_validates_email_and_phone(client, items, event_date):
    done_delivery(client, items, event_date)

    response = client.post(step_url("contact"), contact_data(email="nope", phone="555"))

    html = response.content.decode()
    assert "Enter a valid email address." in html
    assert "Enter a phone number with area code." in html


def test_tax_exempt_needs_organization_and_certificate(client, items, event_date):
    done_delivery(client, items, event_date)

    response = client.post(step_url("contact"), contact_data(tax_exempt="on"))

    html = response.content.decode()
    assert "Enter the organization&#x27;s name." in html
    assert "Enter the exemption certificate number." in html


def test_contact_step_says_tax_is_removed_only_after_staff_verify(client, items, event_date):
    done_delivery(client, items, event_date)

    html = client.get(step_url("contact")).content.decode()

    assert "We are a tax-exempt organization" in html
    assert "Tax is removed after our staff verify your exemption certificate" in html


# Step 4: review and submit


def test_review_shows_the_full_quote_and_terms_link(reviewing):
    html = reviewing.get(REVIEW).content.decode()

    assert "Rental total" in html and "Refundable caution deposit" in html
    assert 'href="/rental-terms/"' in html
    assert "Card or bank account (ACH)" in html and "Zelle" in html
    assert "Cash App" in html and "Bank deposit" in html
    assert "1 Main St\nDallas, TX 75001" in html


def test_terms_must_be_accepted(reviewing):
    response = reviewing.post(REVIEW, review_data(accept_terms=""))

    assert response.status_code == 422
    assert "Please read and accept the rental terms to book." in response.content.decode()
    assert not Booking.objects.exists()


def test_booking_is_created_only_through_create_booking_from_quote(reviewing, items, event_date):
    real = quote_service.create_booking_from_quote
    with (
        mock.patch.object(
            quote_service, "create_booking_from_quote", side_effect=real
        ) as create_booking,
        mock.patch.object(
            stripe_service, "create_checkout_session", return_value={"url": "https://x.test/"}
        ),
    ):
        reviewing.post(REVIEW, review_data())

    create_booking.assert_called_once()
    args, kwargs = create_booking.call_args
    lines, called_date, zip_code, _promo = args
    assert called_date == event_date and zip_code == ZIP
    assert {(line.variant_id, line.package_id, line.quantity) for line in lines} == {
        (items["chair"].pk, None, 10),
        (None, items["package"].pk, 1),
    }
    assert kwargs["customer_name"] == "Ada Planner"
    assert Booking.objects.count() == 1


def test_a_cart_that_sold_out_since_review_is_refused(reviewing, items):
    items["chair"].stock_quantity = 5
    items["chair"].save()

    with mock.patch.object(stripe_service, "create_checkout_session") as checkout:
        response = reviewing.post(REVIEW, review_data())

    assert response.status_code == 409
    assert "Your quote changed." in response.content.decode()
    assert not Booking.objects.exists()
    checkout.assert_not_called()


def test_card_path_calls_create_checkout_session_and_redirects_to_stripe(reviewing, settings):
    settings.SITE_URL = "https://rent.example"
    with mock.patch.object(
        stripe_service, "create_checkout_session", return_value={"url": "https://stripe.test/c"}
    ) as checkout:
        response = reviewing.post(REVIEW, review_data())

    assert response.status_code == 303 and response["Location"] == "https://stripe.test/c"
    booking = Booking.objects.get()
    args, kwargs = checkout.call_args
    assert args == (booking, Payment.Kind.DEPOSIT)
    assert kwargs["success_url"].startswith("https://rent.example/payments/success/")
    assert kwargs["success_url"].endswith("?session_id={CHECKOUT_SESSION_ID}")
    assert kwargs["cancel_url"].startswith("https://rent.example/payments/cancel/")
    assert booking.status == Booking.Status.HELD
    assert reviewing.session[cart_service.CART_KEY], "cart kept until payment is confirmed"


def test_pay_in_full_opens_a_full_checkout(reviewing):
    with mock.patch.object(
        stripe_service, "create_checkout_session", return_value={"url": "https://stripe.test/c"}
    ) as checkout:
        reviewing.post(REVIEW, review_data(plan="full"))

    assert checkout.call_args.args[1] == Payment.Kind.FULL


def test_card_path_records_details_terms_and_choice_on_the_booking(
    reviewing, sessions, settings, event_date
):
    settings.TERMS_VERSION = "2026-11-v1"
    before = timezone.now()

    reviewing.post(REVIEW, review_data())

    booking = Booking.objects.get()
    assert booking.terms_version == "2026-11-v1"
    assert before <= booking.terms_accepted_at <= timezone.now()
    assert booking.payment_choice == "stripe" and booking.payment_plan == "deposit"
    assert booking.event_type == "birthday" and booking.surface == "grass"
    assert booking.event_start_time.strftime("%H:%M") == "14:00"
    assert booking.setup_by_time.strftime("%H:%M") == "12:30"
    assert booking.crew_notes == "Gate code 1234"
    assert booking.delivery_address == "1 Main St\nDallas, TX 75001"
    assert booking.event_date == event_date
    payment = booking.payments.get()
    assert payment.status == Payment.Status.PENDING and payment.amount == booking.deposit_amount
    assert len(sessions.create_calls) == 1


def test_failed_checkout_start_releases_the_booking(reviewing):
    with mock.patch.object(
        stripe_service, "create_checkout_session", side_effect=stripe.APIConnectionError("down")
    ):
        response = reviewing.post(REVIEW, review_data())

    assert response.status_code == 502
    assert "Nothing was charged." in response.content.decode()
    assert Booking.objects.get().status == Booking.Status.CANCELLED
    assert reviewing.session[cart_service.CART_KEY]


def test_submitting_again_after_backing_out_of_stripe_releases_the_first_booking(
    reviewing, sessions
):
    reviewing.post(REVIEW, review_data())
    first = Booking.objects.get()

    response = reviewing.post(REVIEW, review_data(plan="full"))

    first.refresh_from_db()
    assert first.status == Booking.Status.CANCELLED
    assert first.payments.get().status == Payment.Status.FAILED
    second = Booking.objects.exclude(pk=first.pk).get()
    assert second.status == Booking.Status.HELD
    assert response.status_code == 303
    assert len(sessions.expire_calls) == 1


def test_a_paid_earlier_booking_is_not_booked_twice(reviewing, sessions):
    reviewing.post(REVIEW, review_data())
    first = Booking.objects.get()
    first.status = Booking.Status.DEPOSIT_PAID
    first.save()

    response = reviewing.post(REVIEW, review_data())

    assert response.status_code == 302 and "/booking/" in response.url
    assert Booking.objects.count() == 1


# Manual payments


@pytest.mark.parametrize("choice", ["zelle", "cashapp", "bank_deposit"])
def test_manual_path_holds_for_manual_pay_hold_hours_and_shows_instructions(
    reviewing, settings, choice
):
    settings.MANUAL_PAY_HOLD_HOURS = 24
    settings.ZELLE_RECIPIENT = "pay@rent.example"
    settings.CASHAPP_TAG = "$RentExample"
    settings.BANK_ACCOUNT_NUMBER = "123456789"
    with mock.patch.object(stripe_service, "create_checkout_session") as checkout:
        response = reviewing.post(REVIEW, review_data(choice=choice))

    checkout.assert_not_called()
    booking = Booking.objects.get()
    assert booking.status == Booking.Status.HELD
    assert booking.payment_choice == choice
    expected = timezone.now() + timedelta(hours=24)
    assert abs(booking.hold_expires_at - expected) < timedelta(seconds=30)
    assert not booking.payments.exists()
    assert cart_service.CART_KEY not in reviewing.session

    page = reviewing.get(response.url)
    html = page.content.decode()
    assert f"#{booking.pk}" in html and f"Booking {booking.pk}" in html
    assert f"${booking.deposit_amount:,.2f}" in html
    deadline = dateformat.format(timezone.localtime(booking.hold_expires_at), "l, M j, Y, g:i A T")
    assert f"Pay by {deadline}" in html
    assert "Our staff confirm your payment once it arrives" in html
    value = {"zelle": "pay@rent.example", "cashapp": "$RentExample"}.get(choice, "123456789")
    assert value in html


def test_manual_hold_follows_the_setting(reviewing, settings):
    settings.MANUAL_PAY_HOLD_HOURS = 6

    reviewing.post(REVIEW, review_data(choice="zelle"))

    booking = Booking.objects.get()
    expected = timezone.now() + timedelta(hours=6)
    assert abs(booking.hold_expires_at - expected) < timedelta(seconds=30)


def test_manual_full_payment_asks_for_total_plus_caution(reviewing):
    response = reviewing.post(REVIEW, review_data(choice="cashapp", plan="full"))

    booking = Booking.objects.get()
    html = reviewing.get(response.url).content.decode()
    assert booking.caution_fee > 0
    assert f"${booking.total + booking.caution_fee:,.2f}" in html
    assert "refundable caution deposit" in html


def test_manual_booking_hold_lapses_like_any_other(reviewing, settings):
    from apps.bookings.services.availability import release_expired_holds

    reviewing.post(REVIEW, review_data(choice="zelle"))
    booking = Booking.objects.get()

    with mock.patch(
        "django.utils.timezone.now", return_value=booking.hold_expires_at + timedelta(minutes=1)
    ):
        release_expired_holds()

    booking.refresh_from_db()
    assert booking.status == Booking.Status.EXPIRED


# Tax exemption


def test_tax_exempt_request_is_stored_without_removing_tax(client, items, event_date, settings):
    settings.SALES_TAX_RATE = Decimal("8.25")
    add(client, "variant", items["chair"].pk, 10)
    set_details(client, event_date)
    fill_steps(
        client,
        event_date,
        contact=contact_data(tax_exempt="on", org_name="First Church", cert_number="TX-123"),
    )
    review = client.get(REVIEW).content.decode()
    assert "Tax exemption requested for First Church." in review

    client.post(REVIEW, review_data(choice="zelle"))

    booking = Booking.objects.get()
    assert booking.tax_exempt_requested is True
    assert booking.tax_exempt_org_name == "First Church"
    assert booking.tax_exempt_cert_number == "TX-123"
    assert booking.tax_exempt is False and booking.tax_exempt_certificate == ""
    assert booking.tax == Decimal("2.89")


def test_tax_exempt_fields_are_dropped_when_box_is_unticked(client, items, event_date):
    add(client, "variant", items["chair"].pk, 1)
    set_details(client, event_date)
    fill_steps(client, event_date, contact=contact_data(org_name="Ignored", cert_number="X"))

    client.post(REVIEW, review_data(choice="zelle"))

    booking = Booking.objects.get()
    assert not booking.tax_exempt_requested
    assert booking.tax_exempt_org_name == "" and booking.tax_exempt_cert_number == ""
