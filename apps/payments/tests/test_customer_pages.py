"""Customer payment pages: success polling, cancel, balance and status links."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.services import booking_flow, links
from apps.bookings.services import cart as cart_service
from apps.bookings.tests.flow_helpers import catalogue, ready_to_review, review_data, step_url
from apps.payments.models import Payment
from apps.payments.tests.helpers import (
    event,
    held_booking,
    post_event,
    session_object,
    stripe_sessions,
)
from apps.payments.views import MAX_POLLS

pytestmark = pytest.mark.django_db

HX = {"HX-Request": "true"}


@pytest.fixture
def booked(client, event_date, settings):
    """A booking submitted through the flow and sent to (fake) Stripe Checkout."""
    settings.SITE_URL = "https://rent.example"
    items = catalogue()
    ready_to_review(client, event_date, items)
    response = client.post(step_url("review"), review_data())
    assert response.status_code == 303
    booking = Booking.objects.get()
    payment = booking.payments.get()
    call = stripe_sessions().create_calls[-1]
    success_path = call["success_url"].removeprefix("https://rent.example")
    success_path = success_path.replace("{CHECKOUT_SESSION_ID}", payment.stripe_checkout_session_id)
    return {"booking": booking, "payment": payment, "success": success_path, "call": call}


def status_path(booked):
    path, query = booked["success"].split("?")
    return path + "status/?" + query


def test_success_page_never_marks_anything_paid(client, booked):
    response = client.get(booked["success"])

    html = response.content.decode()
    assert response.status_code == 200
    assert "Confirming your payment" in html
    assert "hx-get=" in html and "/status/" in html
    booking, payment = booked["booking"], booked["payment"]
    booking.refresh_from_db()
    payment.refresh_from_db()
    assert booking.status == Booking.Status.HELD
    assert payment.status == Payment.Status.PENDING
    assert booking.amount_paid == 0


def test_success_page_shows_confirmed_once_the_webhook_records_payment(client, booked):
    payment = booked["payment"]
    stripe_sessions().complete(payment.stripe_checkout_session_id)
    assert client.get(status_path(booked)).content.decode().count("Confirming your payment") == 1

    post_event(client, event("checkout.session.completed", session_object(payment)))

    html = client.get(status_path(booked)).content.decode()
    assert "Booking confirmed" in html
    assert "hx-get=" not in html, "polling stops once confirmed"
    booked["booking"].refresh_from_db()
    assert booked["booking"].status == Booking.Status.DEPOSIT_PAID


def test_ach_shows_payment_processing(client, booked):
    payment = booked["payment"]
    stripe_sessions().complete(payment.stripe_checkout_session_id, payment_status="unpaid")
    post_event(
        client,
        event("checkout.session.completed", session_object(payment, payment_status="unpaid")),
    )

    html = client.get(booked["success"]).content.decode()

    assert "Payment processing" in html
    assert "usually take 3 to 5 business days" in html
    assert "hx-get=" not in html


def test_failed_payment_is_shown_as_failed(client, booked):
    post_event(
        client, event("checkout.session.async_payment_failed", session_object(booked["payment"]))
    )

    html = client.get(booked["success"]).content.decode()

    assert "Payment did not go through" in html


def test_polling_gives_up_after_max_polls(client, booked):
    html = client.get(status_path(booked) + f"&n={MAX_POLLS}", headers=HX).content.decode()

    assert "taking longer than usual" in html
    assert "hx-get=" not in html


def test_success_page_clears_the_cart_and_flow(client, booked):
    assert client.session.get(cart_service.CART_KEY)

    client.get(booked["success"])

    assert cart_service.CART_KEY not in client.session
    assert booking_flow.FLOW_KEY not in client.session


def test_success_page_leaves_the_cart_for_a_booking_this_visitor_did_not_make(client, booked):
    other = held_booking()
    token = links.make_token(other, links.SUCCESS)

    client.get(reverse("payments:success", args=[token]))

    assert client.session.get(cart_service.CART_KEY)


def test_cancel_returns_to_review_with_the_cart_intact(client, booked):
    cancel_path = booked["call"]["cancel_url"].removeprefix("https://rent.example")

    response = client.get(cancel_path, follow=True)

    assert response.redirect_chain[-1][0] == step_url("review")
    html = response.content.decode()
    assert "Payment cancelled. Nothing was charged." in html
    assert "Book now" in html and "Chair" in html
    assert client.session.get(cart_service.CART_KEY)


# Signed links


def test_tampered_links_are_refused(client, booked):
    booking = booked["booking"]
    token = links.make_token(booking, links.STATUS)
    forged = token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB")

    for name, value in (("bookings:status", forged), ("payments:balance", forged)):
        assert client.get(reverse(name, args=[value])).status_code == 404
    assert client.get(reverse("payments:success", args=["garbage"])).status_code == 404


def test_a_link_for_one_page_does_not_open_another(client, booked):
    status_token = links.make_token(booked["booking"], links.STATUS)

    assert client.get(reverse("payments:balance", args=[status_token])).status_code == 404
    assert client.get(reverse("payments:success", args=[status_token])).status_code == 404


def test_a_link_signed_with_another_key_is_refused(client, booked, settings):
    settings.SECRET_KEY = "another-key"  # noqa: S105
    token = links.make_token(booked["booking"], links.STATUS)
    settings.SECRET_KEY = "dev-only-insecure-key"  # noqa: S105

    assert client.get(reverse("bookings:status", args=[token])).status_code == 404


def test_status_link_expires_after_status_link_days(client, booked, settings):
    settings.STATUS_LINK_DAYS = 180
    issued = timezone.now() - timedelta(days=181)
    token = links.make_token(booked["booking"], links.STATUS, now=issued)
    fresh = links.make_token(
        booked["booking"], links.STATUS, now=timezone.now() - timedelta(days=179)
    )

    assert client.get(reverse("bookings:status", args=[token])).status_code == 410
    assert client.get(reverse("bookings:status", args=[fresh])).status_code == 200


def test_balance_link_lasts_until_the_end_of_the_event_date(settings):
    settings.BALANCE_LINK_DAYS = 14
    booking = held_booking(event_date=timezone.localdate() + timedelta(days=60))

    expires = links.expiry_for(booking, links.BALANCE)

    local = timezone.localtime(expires)
    assert local.date() == booking.event_date and (local.hour, local.minute) == (23, 59)
    token = links.make_token(booking, links.BALANCE)
    with pytest.raises(links.ExpiredLink):
        links.read_token(token, links.BALANCE, now=expires + timedelta(seconds=1))
    assert links.read_token(token, links.BALANCE, now=expires - timedelta(hours=1)) == booking


def test_balance_link_lasts_balance_link_days_when_that_is_later(settings):
    settings.BALANCE_LINK_DAYS = 14
    booking = held_booking(event_date=timezone.localdate() + timedelta(days=3))
    now = timezone.now()

    expires = links.expiry_for(booking, links.BALANCE, now=now)

    assert expires == now + timedelta(days=14)


def test_expired_balance_link_is_refused(client):
    booking = held_booking(event_date=timezone.localdate() - timedelta(days=30))
    token = links.make_token(booking, links.BALANCE, now=timezone.now() - timedelta(days=31))

    response = client.get(reverse("payments:balance", args=[token]))

    assert response.status_code == 410
    assert "This link has expired" in response.content.decode()


# Balance and status pages


def deposit_paid_booking():
    booking = held_booking(total="300.00", caution_fee="25.00")
    add_payment_succeeded(booking, "150.00")
    booking.status = Booking.Status.DEPOSIT_PAID
    booking.hold_expires_at = None
    booking.save()
    return booking


def add_payment_succeeded(booking, amount):
    return Payment.objects.create(
        booking=booking,
        amount=Decimal(amount),
        kind=Payment.Kind.DEPOSIT,
        method=Payment.Method.CARD,
        status=Payment.Status.SUCCEEDED,
        received_at=timezone.now(),
    )


def test_balance_page_shows_balance_and_opens_a_balance_checkout(client, settings):
    settings.SITE_URL = "https://rent.example"
    booking = deposit_paid_booking()
    url = reverse("payments:balance", args=[links.make_token(booking, links.BALANCE)])

    html = client.get(url).content.decode()
    assert "$150.00" in html and "$25.00" in html and "Pay $175.00" in html

    response = client.post(url)

    assert response.status_code == 303 and response["Location"].startswith("https://stripe.test/")
    payment = booking.payments.get(kind=Payment.Kind.BALANCE)
    assert payment.amount == Decimal("150.00") and payment.caution_amount == Decimal("25.00")
    assert payment.status == Payment.Status.PENDING
    call = stripe_sessions().create_calls[-1]
    assert "/payments/success/" in call["success_url"]
    assert call["cancel_url"] == f"https://rent.example{url}"


def test_balance_page_on_a_fully_paid_booking_says_nothing_to_pay(client):
    booking = held_booking(total="100.00")
    add_payment_succeeded(booking, "100.00")
    booking.status = Booking.Status.PAID
    booking.save()
    url = reverse("payments:balance", args=[links.make_token(booking, links.BALANCE)])

    with mock.patch("apps.payments.services.stripe.create_checkout_session") as checkout:
        response = client.post(url)

    checkout.assert_not_called()
    assert "Pay $" not in response.content.decode()
    assert response.status_code == 200


def test_status_page_shows_items_date_address_and_money(client):
    booking = deposit_paid_booking()
    booking.delivery_address = "9 Elm St\nPlano, TX 75023"
    booking.save()
    url = reverse("bookings:status", args=[links.make_token(booking, links.STATUS)])

    html = client.get(url).content.decode()

    item = booking.items.get()
    assert f"1 x {item.variant}" in html
    assert (
        f"{booking.event_date:%A, %b} {booking.event_date.day}, {booking.event_date.year}" in html
    )
    assert "9 Elm St\nPlano, TX 75023" in html
    assert "$300.00" in html and "$150.00" in html
    assert "Deposit paid" in html


def test_manual_instructions_link_is_signed(client):
    booking = held_booking()

    assert client.get(reverse("payments:manual", args=["nope"])).status_code == 404
    token = links.make_token(booking, links.MANUAL)
    assert client.get(reverse("payments:manual", args=[token])).status_code == 200
