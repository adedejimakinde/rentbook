"""Sync with Stripe: settle a booking's pending payments and refunds from what Stripe holds."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
import stripe
from django.urls import reverse

from apps.bookings.models import Booking
from apps.payments.models import Payment
from apps.payments.services import stripe as stripe_service
from apps.payments.tests.helpers import held_booking, pay, start_checkout, stripe_sessions

pytestmark = pytest.mark.django_db

D = Decimal
Status = Booking.Status


def test_paid_session_is_recorded(refunds):
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    stripe_sessions().complete(payment.stripe_checkout_session_id)

    stripe_service.sync_booking(booking)

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert payment.status == Payment.Status.SUCCEEDED
    assert payment.stripe_payment_intent_id == f"pi_{payment.stripe_checkout_session_id}"
    assert booking.status == Status.PAID


def test_expired_session_fails_its_payment(refunds):
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    stripe_sessions().lapse(payment.stripe_checkout_session_id)

    stripe_service.sync_booking(booking)

    payment.refresh_from_db()
    assert payment.status == Payment.Status.FAILED


def test_open_session_is_left_alone(refunds):
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)

    stripe_service.sync_booking(booking)

    payment.refresh_from_db()
    assert payment.status == Payment.Status.PENDING


def test_session_charged_a_different_amount_is_not_credited(refunds):
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    session = stripe_sessions().complete(payment.stripe_checkout_session_id)
    session["amount_total"] = 100

    stripe_service.sync_booking(booking)

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert payment.status == Payment.Status.PENDING
    assert booking.needs_attention


def test_clearing_ach_payment_is_settled_from_its_payment_intent(refunds, intents):
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    session = stripe_sessions().complete(payment.stripe_checkout_session_id, "unpaid")

    stripe_service.sync_booking(booking)
    payment.refresh_from_db()
    assert payment.status == Payment.Status.PENDING
    assert payment.method == Payment.Method.ACH

    intents.statuses[session["payment_intent"]] = "succeeded"
    stripe_service.sync_booking(booking)

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert payment.status == Payment.Status.SUCCEEDED
    assert booking.status == Status.PAID


def test_failed_ach_payment_is_marked_failed(refunds, intents):
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    session = stripe_sessions().complete(payment.stripe_checkout_session_id, "unpaid")
    intents.statuses[session["payment_intent"]] = "requires_payment_method"

    stripe_service.sync_booking(booking)

    payment.refresh_from_db()
    assert payment.status == Payment.Status.FAILED


@pytest.fixture
def paid_with_caution(client):
    booking = held_booking(total="200.00", caution_fee="75.00")
    return booking, pay(client, booking, Payment.Kind.FULL)


def timed_out_refund(refunds, made_at_stripe):
    """A refund call that times out; made_at_stripe says whether Stripe did it anyway."""
    real_create = refunds.create

    def create(**kwargs):
        if made_at_stripe:
            real_create(**kwargs)
        raise stripe.APIConnectionError("timed out")

    return mock.patch.object(refunds, "create", side_effect=create)


def test_refund_left_pending_by_a_timeout_is_settled_when_stripe_made_it(
    paid_with_caution, refunds
):
    booking, payment = paid_with_caution
    with timed_out_refund(refunds, True), pytest.raises(stripe.APIConnectionError):
        stripe_service.refund_rental(payment, D("50.00"))
    entry = booking.payments.get(kind=Payment.Kind.RENTAL_REFUND)
    assert entry.status == Payment.Status.PENDING

    stripe_service.sync_booking(booking)

    entry.refresh_from_db()
    payment.refresh_from_db()
    assert entry.status == Payment.Status.SUCCEEDED
    assert entry.stripe_refund_id == refunds.refunds[0]["id"]
    assert payment.refunded_amount == D("50.00")


def test_old_pending_refund_stripe_never_made_is_failed(paid_with_caution, refunds):
    booking, payment = paid_with_caution
    with timed_out_refund(refunds, False), pytest.raises(stripe.APIConnectionError):
        stripe_service.refund_caution(booking, D("75.00"))
    entry = booking.payments.get(kind=Payment.Kind.CAUTION_REFUND)

    stripe_service.sync_booking(booking)
    entry.refresh_from_db()
    assert entry.status == Payment.Status.PENDING

    minutes = stripe_service.PENDING_REFUND_TIMEOUT_MINUTES + 1
    Payment.objects.filter(pk=entry.pk).update(
        created_at=entry.created_at - timedelta(minutes=minutes)
    )
    stripe_service.sync_booking(booking)

    entry.refresh_from_db()
    booking.refresh_from_db()
    assert entry.status == Payment.Status.FAILED
    assert booking.caution_held == D("75.00")


def test_admin_sync_with_stripe_action(admin_client, refunds):
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    stripe_sessions().complete(payment.stripe_checkout_session_id)

    response = admin_client.post(
        reverse("admin:bookings_booking_changelist"),
        {"action": "sync_with_stripe", "_selected_action": [booking.pk]},
        follow=True,
    )

    assert response.status_code == 200
    assert "Synced 1 booking" in response.content.decode()
    payment.refresh_from_db()
    assert payment.status == Payment.Status.SUCCEEDED


def test_admin_sync_reports_stripe_errors(admin_client, refunds):
    booking = held_booking(total="200.00")
    start_checkout(booking, Payment.Kind.FULL)
    down = stripe.APIConnectionError("down")

    with mock.patch.object(stripe_sessions(), "retrieve", side_effect=down):
        response = admin_client.post(
            reverse("admin:bookings_booking_changelist"),
            {"action": "sync_with_stripe", "_selected_action": [booking.pk]},
            follow=True,
        )

    assert f"Booking #{booking.pk}" in response.content.decode()
    assert "could not be synced" in response.content.decode()
