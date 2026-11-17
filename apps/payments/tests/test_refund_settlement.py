"""Refund entries are settled from what Stripe holds now, never from an old list."""

import copy
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
import stripe

from apps.bookings.models import Booking
from apps.payments.models import Payment
from apps.payments.services import stripe as stripe_service
from apps.payments.tests.helpers import FakeList, event, held_booking, pay, post_event

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture
def paid(client):
    """A $200 booking paid in full by card."""
    booking = held_booking(total="200.00")
    return booking, pay(client, booking, Payment.Kind.FULL)


def rental_entry(booking):
    return booking.payments.get(kind=Payment.Kind.RENTAL_REFUND)


def test_an_older_overlapping_sync_cannot_undo_a_refund_stripe_failed(client, paid, refunds):
    """Sync A reads the list while the refund is pending; refund.failed lands before A locks.

    A must not take its older list's word over the failure recorded in between.
    """
    booking, payment = paid
    stripe_service.refund_rental(payment, D("60.00"))
    refunds.refunds[0]["status"] = "pending"
    real_list = refunds.list
    calls = []

    def older_list_then_failure(**kwargs):
        calls.append(kwargs)
        if len(calls) > 1:
            return real_list(**kwargs)
        older = FakeList(refunds, kwargs, copy.deepcopy(refunds.refunds), has_more=False)
        refunds.refunds[0]["status"] = "failed"
        assert post_event(client, event("refund.failed", refunds.refunds[0])).status_code == 200
        return older

    with mock.patch.object(refunds, "list", side_effect=older_list_then_failure):
        stripe_service.sync_refunds(payment)

    payment.refresh_from_db()
    booking.refresh_from_db()
    entry = rental_entry(booking)
    assert entry.status == Payment.Status.FAILED
    assert entry.failure_reason == "stripe_failed"
    assert payment.refunded_amount == D("0.00")
    assert booking.amount_paid == D("200.00")
    assert booking.needs_attention


def test_each_refund_is_fetched_again_under_the_lock(paid, refunds):
    booking, payment = paid
    stripe_service.refund_rental(payment, D("60.00"))
    refunds.retrieve_calls.clear()

    stripe_service.sync_refunds(payment)

    assert refunds.retrieve_calls == [refunds.refunds[0]["id"]]


def test_a_refund_stripe_reported_failed_is_never_moved_back_to_succeeded(client, paid, refunds):
    booking, payment = paid
    stripe_service.refund_rental(payment, D("60.00"))
    refunds.refunds[0]["status"] = "failed"
    post_event(client, event("refund.failed", refunds.refunds[0]))
    booking.refresh_from_db()
    booking.needs_attention = False
    booking.save()

    refunds.refunds[0]["status"] = "succeeded"
    stripe_service.sync_refunds(payment)

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert rental_entry(booking).status == Payment.Status.FAILED
    assert payment.refunded_amount == D("0.00")
    assert booking.needs_attention


def age(entry, minutes):
    Payment.objects.filter(pk=entry.pk).update(
        created_at=entry.created_at - timedelta(minutes=minutes)
    )


def test_a_refund_the_app_failed_after_a_timeout_is_revived_when_stripe_has_it(paid, refunds):
    """The app failed it itself (nothing at Stripe yet), so a later sighting is believed."""
    booking, payment = paid
    refunds.on_create = mock.Mock(side_effect=stripe.APIConnectionError("timed out"))
    with pytest.raises(stripe.APIConnectionError):
        stripe_service.refund_rental(payment, D("60.00"))
    entry = rental_entry(booking)
    age(entry, stripe_service.PENDING_REFUND_TIMEOUT_MINUTES + 1)
    stripe_service.sync_refunds(payment)
    entry.refresh_from_db()
    assert entry.status == Payment.Status.FAILED
    assert entry.failure_reason == "not_received"

    refunds.refunds.append(
        {
            "id": "re_late",
            "object": "refund",
            "payment_intent": payment.stripe_payment_intent_id,
            "amount": 6000,
            "status": "succeeded",
            "metadata": {"kind": stripe_service.RENTAL_REFUND, "entry_id": str(entry.pk)},
        }
    )
    stripe_service.sync_refunds(payment)

    entry.refresh_from_db()
    payment.refresh_from_db()
    assert entry.status == Payment.Status.SUCCEEDED
    assert entry.failure_reason == ""
    assert payment.refunded_amount == D("60.00")


def reaches_stripe_then_raises(refunds, error):
    """Stripe makes the refund but the answer is an error: the outcome is unknown to us."""

    def create(kwargs):
        refunds.refunds.append(
            {
                "id": "re_unknown",
                "object": "refund",
                "payment_intent": kwargs["payment_intent"],
                "amount": kwargs["amount"],
                "status": "succeeded",
                "metadata": kwargs["metadata"],
            }
        )
        raise error

    return create


def test_a_stripe_server_error_keeps_the_refund_pending_for_the_sync(paid, refunds):
    booking, payment = paid
    refunds.on_create = reaches_stripe_then_raises(
        refunds, stripe.APIError("Stripe had a problem", http_status=500)
    )

    with pytest.raises(stripe.APIError):
        stripe_service.refund_rental(payment, D("60.00"))

    entry = rental_entry(booking)
    booking.refresh_from_db()
    assert entry.status == Payment.Status.PENDING
    assert booking.needs_attention

    stripe_service.sync_booking(booking)

    entry.refresh_from_db()
    payment.refresh_from_db()
    assert entry.status == Payment.Status.SUCCEEDED
    assert payment.refunded_amount == D("60.00")


def test_a_stripe_server_error_reserves_the_money_until_settled(paid, refunds):
    booking, payment = paid
    refunds.on_create = mock.Mock(side_effect=stripe.APIError("Bad gateway", http_status=502))
    with pytest.raises(stripe.APIError):
        stripe_service.refund_rental(payment, D("150.00"))
    refunds.on_create = None

    payment.refresh_from_db()
    assert payment.rental_refundable == D("50.00")


@pytest.mark.parametrize(
    "error",
    [
        stripe.InvalidRequestError("Charge already refunded", None, http_status=400),
        stripe.PermissionError("Not allowed", http_status=403),
        stripe.RateLimitError("Too many requests", http_status=429),
    ],
)
def test_a_4xx_from_stripe_fails_the_refund_entry(paid, refunds, error):
    booking, payment = paid
    refunds.on_create = mock.Mock(side_effect=error)

    with pytest.raises(type(error)):
        stripe_service.refund_rental(payment, D("60.00"))

    entry = rental_entry(booking)
    payment.refresh_from_db()
    assert entry.status == Payment.Status.FAILED
    assert entry.failure_reason == "stripe_refused"
    assert payment.rental_refundable == D("200.00")


def test_failed_entries_say_why(client, paid, refunds):
    booking, payment = paid
    stripe_service.refund_rental(payment, D("60.00"))
    refunds.refunds[0]["status"] = "failed"
    post_event(client, event("refund.failed", refunds.refunds[0]))

    assert rental_entry(booking).failure_reason == "stripe_failed"
    assert Booking.objects.get(pk=booking.pk).needs_attention
