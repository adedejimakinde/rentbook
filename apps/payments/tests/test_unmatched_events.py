"""Webhook events that match no payment are kept and matched again later."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.conf import settings as django_settings
from django.urls import reverse

from apps.bookings.models import Booking
from apps.payments import tasks
from apps.payments.models import Payment, UnmatchedStripeEvent
from apps.payments.services import stripe as stripe_service
from apps.payments.tests.helpers import (
    charge_refunded_event,
    event,
    held_booking,
    pay,
    post_event,
    session_object,
    start_checkout,
)

pytestmark = pytest.mark.django_db

D = Decimal


def test_checkout_event_matching_no_payment_is_kept_and_credited_on_retry(client):
    """An event whose session id matches no payment is kept, then credited once one matches.

    The payment's session id is cleared to stand in for any reason the lookup
    misses. The event's metadata still names the payment, and is not used to
    match it: events are matched by session id only. Once the session id is on
    the payment, the retry credits it, resolves the event and does not run it
    again.
    """
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    session = session_object(payment)
    Payment.objects.filter(pk=payment.pk).update(stripe_checkout_session_id=None)

    response = post_event(client, event("checkout.session.completed", session))

    assert response.status_code == 200
    stored = UnmatchedStripeEvent.objects.get()
    assert stored.type == "checkout.session.completed"
    assert stored.object_id == session["id"]
    assert stored.resolved_at is None
    payment.refresh_from_db()
    assert payment.status == Payment.Status.PENDING

    Payment.objects.filter(pk=payment.pk).update(stripe_checkout_session_id=session["id"])
    assert stripe_service.retry_unmatched() == 1

    payment.refresh_from_db()
    booking.refresh_from_db()
    stored.refresh_from_db()
    assert payment.status == Payment.Status.SUCCEEDED
    assert booking.status == Booking.Status.PAID
    assert stored.resolved_at is not None
    assert stripe_service.retry_unmatched() == 0


def test_refund_event_for_an_unknown_charge_is_kept(client, refunds):
    booking = held_booking(total="200.00")
    payment = pay(client, booking, Payment.Kind.FULL)
    refunds.add(payment, "50.00")
    payload = charge_refunded_event(payment, refunds)
    intent = payment.stripe_payment_intent_id
    Payment.objects.filter(pk=payment.pk).update(stripe_payment_intent_id=None)

    post_event(client, payload)
    assert UnmatchedStripeEvent.objects.get().object_id == payload["data"]["object"]["id"]

    Payment.objects.filter(pk=payment.pk).update(stripe_payment_intent_id=intent)
    stripe_service.retry_unmatched()

    payment.refresh_from_db()
    assert payment.refunded_amount == D("50.00")
    assert UnmatchedStripeEvent.objects.get().resolved_at is not None


@pytest.mark.parametrize(
    "event_type",
    [
        "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
        "checkout.session.expired",
    ],
)
def test_other_session_events_matching_nothing_are_kept(client, event_type):
    session = {"id": "cs_unknown", "object": "checkout.session", "metadata": {}}
    post_event(client, event(event_type, session))
    assert UnmatchedStripeEvent.objects.get().type == event_type


def test_refund_failed_matching_nothing_is_kept(client):
    refund = {"id": "re_unknown", "object": "refund", "payment_intent": "pi_x", "status": "failed"}
    post_event(client, event("refund.failed", refund))
    assert UnmatchedStripeEvent.objects.get().object_id == "re_unknown"


def test_matched_and_unhandled_events_are_not_kept(client):
    booking = held_booking(total="200.00")
    pay(client, booking, Payment.Kind.FULL)
    post_event(client, event("customer.created", {"id": "cus_1", "object": "customer"}))
    assert not UnmatchedStripeEvent.objects.exists()


def test_a_repeated_unmatched_event_is_kept_once(client):
    payload = event("refund.failed", {"id": "re_unknown", "object": "refund", "payment_intent": ""})
    post_event(client, payload)
    post_event(client, payload)
    assert UnmatchedStripeEvent.objects.count() == 1


def test_retry_counts_attempts_and_gives_up_after_the_limit(client):
    post_event(client, event("refund.failed", {"id": "re_x", "object": "refund"}))

    for _ in range(stripe_service.UNMATCHED_RETRY_LIMIT + 3):
        stripe_service.retry_unmatched()

    stored = UnmatchedStripeEvent.objects.get()
    assert stored.attempts == stripe_service.UNMATCHED_RETRY_LIMIT
    assert stored.resolved_at is None


def test_unmatched_retry_and_stale_sync_are_scheduled_every_15_minutes():
    schedule = django_settings.CELERY_BEAT_SCHEDULE
    assert schedule["retry-unmatched-stripe-events"]["schedule"] == 15 * 60
    assert schedule["sync-stale-payments"]["schedule"] == 15 * 60


def test_scheduled_jobs_retry_unmatched_events_and_sync_stale_payments(client, refunds):
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    stripe_service.stripe.checkout.Session.complete(payment.stripe_checkout_session_id)
    stale = stripe_service.SYNC_AFTER_MINUTES + 1
    Payment.objects.filter(pk=payment.pk).update(
        created_at=payment.created_at - timedelta(minutes=stale)
    )
    post_event(client, event("refund.failed", {"id": "re_x", "object": "refund"}))

    assert tasks.retry_unmatched_stripe_events() == 0
    assert tasks.sync_stale_payments() == 1

    payment.refresh_from_db()
    assert payment.status == Payment.Status.SUCCEEDED
    assert UnmatchedStripeEvent.objects.get().attempts == 1


def test_stale_sync_leaves_fresh_checkouts_alone():
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    stripe_service.stripe.checkout.Session.complete(payment.stripe_checkout_session_id)

    tasks.sync_stale_payments()

    payment.refresh_from_db()
    assert payment.status == Payment.Status.PENDING


def test_unmatched_events_are_read_only_in_the_admin(admin_client, client):
    post_event(client, event("refund.failed", {"id": "re_x", "object": "refund"}))
    stored = UnmatchedStripeEvent.objects.get()

    assert (
        admin_client.get(reverse("admin:payments_unmatchedstripeevent_changelist")).status_code
        == 200
    )
    assert admin_client.get(reverse("admin:payments_unmatchedstripeevent_add")).status_code == 403
    change = reverse("admin:payments_unmatchedstripeevent_change", args=[stored.pk])
    assert admin_client.post(change, {"type": "x"}).status_code == 403
