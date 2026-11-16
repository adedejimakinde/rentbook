"""Races on one booking, run for real: threads, separate connections, real Postgres.

Each test lines its threads up on a barrier so they hit the code at the same
moment. A fake Stripe that is slow at the right point holds the window open,
so a missing lock shows up as a wrong result rather than as a lucky pass.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import OperationalError, connection, transaction
from django.utils import timezone

from apps.bookings.models import Booking
from apps.payments.models import Payment, StripeEvent
from apps.payments.services import ledger
from apps.payments.services import stripe as stripe_service
from apps.payments.tests.helpers import (
    URLS,
    event,
    held_booking,
    pay,
    run_together,
    session_object,
    start_checkout,
)

pytestmark = pytest.mark.django_db(transaction=True)

D = Decimal
WINDOW_SECONDS = 0.5


def meet_inside_stripe(parties=2):
    """A hook for a fake Stripe call that waits for every thread to be in it.

    Unlocked code lets all the threads reach Stripe together. Correctly locked
    code lets one in at a time, so the wait simply times out.
    """
    barrier = threading.Barrier(parties)

    def hook(*args, **kwargs):
        try:
            barrier.wait(timeout=WINDOW_SECONDS)
        except threading.BrokenBarrierError:
            pass

    return hook


def from_another_connection(call):
    """Run call on a separate database connection and return its result."""

    def run():
        try:
            return call()
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(run).result()


def booking_is_locked(booking_id):
    """True when another transaction holds the booking's row lock."""

    def probe():
        try:
            with transaction.atomic():
                list(Booking.objects.select_for_update(nowait=True).filter(pk=booking_id))
        except OperationalError:
            return True
        return False

    return from_another_connection(probe)


def test_same_event_delivered_at_once_is_processed_once():
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.DEPOSIT)
    payload = event("checkout.session.completed", session_object(payment))

    results = run_together(*[lambda: stripe_service.handle_event(payload)] * 4)

    assert sorted(results) == [False, False, False, True]
    booking.refresh_from_db()
    assert booking.amount_paid == D("100.00")
    assert booking.status == Booking.Status.DEPOSIT_PAID
    assert StripeEvent.objects.count() == 1


def test_two_events_for_one_payment_landing_at_once_credit_it_once():
    """Stripe reports one ACH payment twice (completed, then cleared) under different ids."""
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.DEPOSIT)
    completed = event("checkout.session.completed", session_object(payment))
    cleared = event("checkout.session.async_payment_succeeded", session_object(payment))

    results = run_together(
        lambda: stripe_service.handle_event(completed),
        lambda: stripe_service.handle_event(cleared),
    )

    assert results == [True, True]
    booking.refresh_from_db()
    assert booking.payments.count() == 1
    assert booking.amount_paid == D("100.00")
    assert booking.status == Booking.Status.DEPOSIT_PAID


def test_double_click_on_pay_opens_one_checkout(sessions):
    booking = held_booking(total="200.00")
    sessions.on_create = meet_inside_stripe()

    def click():
        return stripe_service.create_checkout_session(booking, Payment.Kind.DEPOSIT, **URLS)

    first, second = run_together(click, click)

    assert first["id"] == second["id"]
    assert len(sessions.create_calls) == 1
    assert booking.payments.count() == 1
    assert booking.payments.get().stripe_checkout_session_id == first["id"]


def test_two_different_checkouts_at_once_leave_one_payable_session(sessions):
    """Deposit in one tab, pay-in-full in another: only one may stay open at Stripe."""
    booking = held_booking(total="200.00")
    sessions.on_create = meet_inside_stripe()

    results = run_together(
        lambda: stripe_service.create_checkout_session(booking, Payment.Kind.DEPOSIT, **URLS),
        lambda: stripe_service.create_checkout_session(booking, Payment.Kind.FULL, **URLS),
    )

    assert not [result for result in results if isinstance(result, Exception)]
    open_at_stripe = [s["id"] for s in sessions.sessions.values() if s["status"] == "open"]
    open_in_ledger = [p.stripe_checkout_session_id for p in booking.payments.open_checkouts()]
    assert len(open_at_stripe) == 1
    assert open_in_ledger == open_at_stripe


def test_booking_stays_locked_until_the_checkout_is_saved_with_its_session_id(sessions):
    """While Stripe is being called nobody else can act on the booking, and no
    payment without a session id is ever visible to another transaction."""
    booking = held_booking(total="200.00")
    first, _ = start_checkout(booking, Payment.Kind.DEPOSIT)
    seen = {}

    def unsaved_payment_visible():
        return booking.payments.filter(stripe_checkout_session_id__isnull=True).exists()

    real_expire = sessions.expire

    def expire(session_id, **kwargs):
        seen["locked_during_expire"] = booking_is_locked(booking.pk)
        return real_expire(session_id, **kwargs)

    def on_create(kwargs):
        seen["locked_during_create"] = booking_is_locked(booking.pk)
        seen["unsaved_visible"] = from_another_connection(unsaved_payment_visible)

    sessions.expire = expire
    sessions.on_create = on_create
    session = stripe_service.create_checkout_session(booking, Payment.Kind.FULL, **URLS)

    assert seen == {
        "locked_during_expire": True,
        "locked_during_create": True,
        "unsaved_visible": False,
    }
    assert not booking_is_locked(booking.pk)
    assert booking.payments.open_checkouts().get().stripe_checkout_session_id == session["id"]


def test_manual_payment_and_checkout_at_once_cannot_both_take_the_money(sessions):
    booking = held_booking(total="200.00")
    staff = get_user_model().objects.create_superuser("owner", "owner@example.com", "pw")

    def manual():
        return ledger.record_manual_payment(
            booking,
            method=Payment.Method.ZELLE,
            amount=D("200.00"),
            received_at=timezone.now(),
            reference="ZL-1",
            received_by=staff,
        )

    results = run_together(
        manual, lambda: stripe_service.create_checkout_session(booking, Payment.Kind.FULL, **URLS)
    )

    refused = [result for result in results if isinstance(result, ledger.PaymentError)]
    assert len(refused) == 1
    booking.refresh_from_db()
    paid_by_hand = booking.amount_paid == D("200.00")
    assert paid_by_hand != booking.payments.open_checkouts().exists()


@pytest.fixture
def paid_with_caution(client):
    """A booking paid in full by card: $200 rental plus $75 caution in one charge."""
    booking = held_booking(total="200.00", caution_fee="75.00")
    payment = pay(client, booking, Payment.Kind.FULL)
    return booking, payment


def test_two_caution_refunds_at_once_cannot_exceed_the_caution_held(paid_with_caution, refunds):
    """Two staff refund $75 and $50 of a $75 caution deposit at the same moment."""
    booking, _ = paid_with_caution
    refunds.on_create = meet_inside_stripe()

    results = run_together(
        lambda: stripe_service.refund_caution(booking, D("75.00")),
        lambda: stripe_service.refund_caution(booking, D("50.00")),
    )

    refused = [result for result in results if isinstance(result, ledger.PaymentError)]
    assert len(refused) == 1
    assert len(refunds.create_calls) == 1
    assert booking.caution_held >= D("0.00")
    assert booking.caution_held == D("75.00") - D(refunds.create_calls[0]["amount"]) / 100


def test_two_rental_refunds_at_once_cannot_spill_into_the_caution(paid_with_caution, refunds):
    """Two refunds of the whole $200 rental at once must not also send back the caution."""
    booking, payment = paid_with_caution
    refunds.on_create = meet_inside_stripe()

    results = run_together(
        lambda: stripe_service.refund_rental(payment, D("200.00")),
        lambda: stripe_service.refund_rental(payment, D("200.00")),
    )

    refused = [result for result in results if isinstance(result, ledger.PaymentError)]
    assert len(refused) == 1
    assert len(refunds.create_calls) == 1
    payment.refresh_from_db()
    assert payment.refunded_amount == D("200.00")
    assert booking.caution_held == D("75.00")
