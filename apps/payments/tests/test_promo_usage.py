"""A promo code is used up by a booking's first successful payment, not by its quote."""

from decimal import Decimal as D

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.services.quote import LineRequest, create_booking_from_quote
from apps.bookings.tests.factories import (
    EVENT_DATE,
    DeliveryZoneFactory,
    ItemVariantFactory,
    PromoCodeFactory,
)
from apps.payments.models import Payment
from apps.payments.services import ledger
from apps.payments.services import stripe as stripe_service
from apps.payments.tests.helpers import (
    event,
    pay,
    post_event,
    run_together,
    session_object,
    start_checkout,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def promo(settings):
    settings.SALES_TAX_RATE = D("0")
    DeliveryZoneFactory(zip_codes=["75001"], delivery_fee=D("0.00"), setup_fee=D("0.00"))
    return PromoCodeFactory(code="ONCE", percent_off=D("10"), max_uses=1)


@pytest.fixture
def staff():
    return get_user_model().objects.create_user("owner", is_staff=True)


def booking_with_promo(code="ONCE"):
    """A held booking for $200 of items, $180 after the 10% promo."""
    variant = ItemVariantFactory(item__base_price=D("200.00"))
    return create_booking_from_quote(
        [LineRequest(variant_id=variant.pk, quantity=1)],
        EVENT_DATE,
        "75001",
        code,
        customer_name="Ada Planner",
        email="ada@example.com",
        phone="555-0100",
        delivery_address="1 Main St, Dallas, TX",
    )


def manual(booking, amount, staff, reference="ZELLE-1"):
    return ledger.record_manual_payment(
        booking,
        method=Payment.Method.ZELLE,
        amount=D(amount),
        received_at=timezone.now(),
        reference=reference,
        received_by=staff,
    )


def test_promo_is_counted_once_on_the_first_successful_payment(client, promo):
    booking = booking_with_promo()
    promo.refresh_from_db()
    assert promo.uses_count == 0

    deposit = pay(client, booking, Payment.Kind.DEPOSIT)
    promo.refresh_from_db()
    assert deposit.amount == D("90.00")
    assert promo.uses_count == 1
    assert booking.promo_redeemed_at is not None
    assert booking.needs_attention is False

    pay(client, booking, Payment.Kind.BALANCE)
    promo.refresh_from_db()
    assert booking.status == Booking.Status.PAID
    assert promo.uses_count == 1


def test_duplicate_webhook_does_not_count_the_promo_twice(client, promo):
    promo.max_uses = None
    promo.save()
    booking = booking_with_promo()
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    payload = event("checkout.session.completed", session_object(payment))

    post_event(client, payload)
    post_event(client, payload)
    ledger.record_success(payment.pk)

    promo.refresh_from_db()
    assert promo.uses_count == 1


def test_abandoned_and_failed_checkouts_do_not_use_the_promo(client, promo):
    abandoned = booking_with_promo()
    start_checkout(abandoned, Payment.Kind.DEPOSIT)
    failed = booking_with_promo()
    payment, _ = start_checkout(failed, Payment.Kind.DEPOSIT)
    ledger.record_failure(payment.pk)

    promo.refresh_from_db()
    assert promo.uses_count == 0

    pay(client, failed, Payment.Kind.DEPOSIT)
    promo.refresh_from_db()
    assert promo.uses_count == 1


def test_manual_payment_counts_the_promo(promo, staff):
    booking = booking_with_promo()

    manual(booking, "90.00", staff)
    manual(booking, "90.00", staff, reference="ZELLE-2")

    promo.refresh_from_db()
    assert promo.uses_count == 1


def test_first_payment_is_refused_once_the_promo_is_used_up(client, promo, staff):
    winner = booking_with_promo()
    late = booking_with_promo()
    pay(client, winner, Payment.Kind.DEPOSIT)

    with pytest.raises(ledger.PaymentError, match="maximum number of times"):
        start_checkout(late, Payment.Kind.DEPOSIT)
    with pytest.raises(ledger.PaymentError, match="maximum number of times"):
        manual(late, "90.00", staff)

    assert late.payments.count() == 0
    pay(client, winner, Payment.Kind.BALANCE)
    assert winner.status == Booking.Status.PAID


@pytest.mark.django_db(transaction=True)
def test_payment_that_races_past_the_check_is_kept_and_flagged(client, promo):
    """Two bookings open a checkout while the code has one use left, then both pay at once.

    The two webhooks run in separate threads on separate connections. Both
    payments are kept; one booking takes the last use, the other is flagged,
    and the code is never counted past its limit.
    """
    bookings = [booking_with_promo(), booking_with_promo()]
    payments = [start_checkout(booking, Payment.Kind.DEPOSIT)[0] for booking in bookings]
    payloads = [event("checkout.session.completed", session_object(p)) for p in payments]

    results = run_together(*[lambda p=p: stripe_service.handle_event(p) for p in payloads])

    assert results == [True, True]
    promo.refresh_from_db()
    assert promo.uses_count == 1
    for booking in bookings:
        booking.refresh_from_db()
        assert booking.amount_paid == D("90.00")
        assert booking.status == Booking.Status.DEPOSIT_PAID
        assert booking.promo_redeemed_at is not None
    assert sorted(booking.needs_attention for booking in bookings) == [False, True]

    late = next(booking for booking in bookings if booking.needs_attention)
    pay(client, late, Payment.Kind.BALANCE)
    promo.refresh_from_db()
    assert late.status == Booking.Status.PAID
    assert promo.uses_count == 1


def test_booking_without_a_promo_pays_as_before(client, promo):
    booking = booking_with_promo(code=None)

    pay(client, booking, Payment.Kind.FULL)

    promo.refresh_from_db()
    assert booking.total == D("200.00")
    assert booking.promo_redeemed_at is None
    assert promo.uses_count == 0


def test_a_refund_that_fails_takes_the_promo_use_back(client, promo, refunds):
    """A full refund gave the use back; the refund failing puts the money and the use back."""
    booking = booking_with_promo()
    payment = pay(client, booking, Payment.Kind.FULL)
    stripe_service.refund_rental(payment, payment.amount)
    promo.refresh_from_db()
    booking.refresh_from_db()
    assert promo.uses_count == 0
    assert booking.promo_redeemed_at is None

    refunds.refunds[0]["status"] = "failed"
    post_event(client, event("refund.failed", refunds.refunds[0]))

    promo.refresh_from_db()
    booking.refresh_from_db()
    assert booking.amount_paid == D("180.00")
    assert promo.uses_count == 1
    assert booking.promo_redeemed_at is not None
