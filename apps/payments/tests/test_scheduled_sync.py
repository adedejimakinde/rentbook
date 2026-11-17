"""The scheduled Stripe jobs: unmatched events, stale payments and recently paid bookings."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
import stripe
from django.conf import settings as django_settings
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.tests.factories import BookingFactory
from apps.bookings.tests.helpers import add_payment
from apps.payments import tasks
from apps.payments.models import Payment, UnmatchedStripeEvent
from apps.payments.services import stripe as stripe_service
from apps.payments.tests.helpers import (
    event,
    held_booking,
    post_event,
    session_object,
    start_checkout,
)

pytestmark = pytest.mark.django_db

D = Decimal
Status = Booking.Status


def stripe_paid(event_date, changed_days_ago):
    """A booking paid by card, last changed changed_days_ago."""
    booking = BookingFactory(status=Status.PAID, event_date=event_date)
    payment = add_payment(
        booking,
        "100.00",
        method=Payment.Method.CARD,
        stripe_payment_intent_id=f"pi_{booking.pk}",
    )
    then = timezone.now() - timedelta(days=changed_days_ago)
    Booking.objects.filter(pk=booking.pk).update(updated_at=then)
    Payment.objects.filter(pk=payment.pk).update(updated_at=then)
    return booking


def test_recently_changed_or_soon_paid_bookings_are_synced():
    today = timezone.localdate()
    changed = stripe_paid(today + timedelta(days=90), changed_days_ago=3)
    soon = stripe_paid(today + timedelta(days=10), changed_days_ago=60)
    stripe_paid(today + timedelta(days=90), changed_days_ago=60)
    stripe_paid(today - timedelta(days=5), changed_days_ago=60)
    manual = BookingFactory(status=Status.PAID, event_date=today + timedelta(days=5))
    add_payment(manual, "100.00")

    with mock.patch.object(stripe_service, "sync_booking") as sync:
        assert stripe_service.sync_recent_paid_bookings() == 2

    assert sorted(call.args[0].pk for call in sync.call_args_list) == sorted([changed.pk, soon.pk])


def test_one_booking_stripe_cannot_reach_does_not_stop_the_rest():
    today = timezone.localdate()
    first = stripe_paid(today + timedelta(days=3), changed_days_ago=1)
    second = stripe_paid(today + timedelta(days=4), changed_days_ago=1)

    def sync(booking):
        if booking.pk == first.pk:
            raise stripe.APIConnectionError("down")

    with mock.patch.object(stripe_service, "sync_booking", side_effect=sync) as called:
        assert stripe_service.sync_recent_paid_bookings() == 1
    assert {call.args[0].pk for call in called.call_args_list} == {first.pk, second.pk}


def test_the_jobs_are_separate_tasks_on_their_own_schedules():
    schedule = {
        entry["task"]: entry["schedule"] for entry in django_settings.CELERY_BEAT_SCHEDULE.values()
    }
    assert schedule["payments.retry_unmatched_stripe_events"] == 15 * 60
    assert schedule["payments.sync_stale_payments"] == 15 * 60
    assert schedule["payments.sync_recent_paid_bookings"] == 60 * 60
    assert "payments.sync_stripe" not in schedule

    with (
        mock.patch.object(stripe_service, "retry_unmatched", return_value=1) as retry,
        mock.patch.object(stripe_service, "sync_stale_bookings", return_value=2) as stale,
        mock.patch.object(stripe_service, "sync_recent_paid_bookings", return_value=3) as recent,
    ):
        assert tasks.retry_unmatched_stripe_events() == 1
        retry.assert_called_once()
        stale.assert_not_called()
        assert tasks.sync_stale_payments() == 2
        assert tasks.sync_recent_paid_bookings() == 3
        recent.assert_called_once()


def unmatched_checkout(client):
    """A completed-checkout event kept because its session id matched nothing."""
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    session = session_object(payment)
    Payment.objects.filter(pk=payment.pk).update(stripe_checkout_session_id=None)
    post_event(client, event("checkout.session.completed", session))
    Payment.objects.filter(pk=payment.pk).update(stripe_checkout_session_id=session["id"])
    return UnmatchedStripeEvent.objects.get(object_id=session["id"])


def test_an_unexpected_error_on_one_unmatched_event_is_logged_and_counted(client, caplog):
    broken = unmatched_checkout(client)
    healthy = unmatched_checkout(client)
    real = stripe_service.HANDLERS["checkout.session.completed"]

    def handler(session):
        if session["id"] == broken.object_id:
            raise ValueError("unexpected payload")
        return real(session)

    with mock.patch.dict(stripe_service.HANDLERS, {"checkout.session.completed": handler}):
        assert stripe_service.retry_unmatched() == 1

    broken.refresh_from_db()
    healthy.refresh_from_db()
    assert broken.attempts == 1
    assert broken.resolved_at is None
    assert healthy.resolved_at is not None
    record = next(r for r in caplog.records if "could not be retried" in r.getMessage())
    assert record.levelname == "ERROR"
    assert record.exc_info[0] is ValueError


def test_metadata_alone_never_claims_a_payment(client):
    """Only the session id matches an event to a payment; the metadata fallback is gone."""
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    session = session_object(payment)
    Payment.objects.filter(pk=payment.pk).update(stripe_checkout_session_id=None)

    post_event(client, event("checkout.session.completed", session))

    payment.refresh_from_db()
    assert payment.stripe_checkout_session_id is None
    assert payment.status == Payment.Status.PENDING
    assert UnmatchedStripeEvent.objects.filter(object_id=session["id"]).exists()
