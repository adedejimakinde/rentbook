"""Simultaneous webhook deliveries must credit a payment exactly once."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from django.db import connection

from apps.bookings.models import Booking
from apps.payments.models import Payment, StripeEvent
from apps.payments.services import stripe as stripe_service
from apps.payments.tests.helpers import event, held_booking, session_object, start_checkout

pytestmark = pytest.mark.django_db(transaction=True)


def _deliver(payload):
    try:
        return stripe_service.handle_event(payload)
    finally:
        connection.close()


def test_same_event_delivered_at_once_is_processed_once():
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.DEPOSIT)
    payload = event("checkout.session.completed", session_object(payment))

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_deliver, [payload] * 4))

    assert sorted(results) == [False, False, False, True]
    booking.refresh_from_db()
    assert booking.amount_paid == Decimal("100.00")
    assert booking.status == Booking.Status.DEPOSIT_PAID
    assert StripeEvent.objects.count() == 1


def test_deposit_and_balance_landing_at_once_both_count():
    booking = held_booking(total="200.00")
    first, _ = start_checkout(booking, Payment.Kind.DEPOSIT)
    second = Payment.objects.create(
        booking=booking,
        amount=Decimal("100.00"),
        kind=Payment.Kind.BALANCE,
        method=Payment.Method.CARD,
        stripe_checkout_session_id="cs_parallel",
    )
    payloads = [
        event("checkout.session.completed", session_object(payment)) for payment in (first, second)
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(_deliver, payloads)) == [True, True]

    booking.refresh_from_db()
    assert booking.amount_paid == Decimal("200.00")
    assert booking.status == Booking.Status.PAID
