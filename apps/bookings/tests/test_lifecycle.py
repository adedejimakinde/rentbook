"""Booking status and line changes go through services that check the money first."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.services import availability, lifecycle
from apps.bookings.tests.factories import (
    EVENT_DATE,
    BookingItemFactory,
)
from apps.bookings.tests.helpers import add_payment, booking_worth
from apps.payments.models import Payment

pytestmark = pytest.mark.django_db

D = Decimal
Status = Booking.Status


@pytest.fixture
def staff():
    return get_user_model().objects.create_superuser("owner", "owner@example.com", "pw")


def test_cancel_releases_stock_and_flags_money_still_held():
    booking = booking_worth("200.00", Status.PAID, caution_fee="75.00", stock=1)
    add_payment(booking, "200.00", caution="75.00")
    variant = booking.items.get().variant

    lifecycle.cancel_booking(booking)

    booking.refresh_from_db()
    assert booking.status == Status.CANCELLED
    assert booking.needs_attention
    assert booking.amount_paid == D("200.00")
    assert availability.available_quantity(variant, EVENT_DATE) == 1


def test_cancel_without_money_is_not_flagged():
    booking = booking_worth(
        "200.00", Status.HELD, hold_expires_at=timezone.now() + timedelta(minutes=15)
    )

    lifecycle.cancel_booking(booking)

    booking.refresh_from_db()
    assert booking.status == Status.CANCELLED
    assert booking.hold_expires_at is None
    assert not booking.needs_attention


def test_cancel_is_refused_while_a_payment_is_in_progress():
    booking = booking_worth("200.00", Status.DEPOSIT_PAID)
    add_payment(booking, "100.00")
    add_payment(booking, "100.00", status=Payment.Status.PENDING, kind=Payment.Kind.BALANCE)

    with pytest.raises(lifecycle.BookingStateError, match="still in progress"):
        lifecycle.cancel_booking(booking)

    booking.refresh_from_db()
    assert booking.status == Status.DEPOSIT_PAID


def test_cancel_is_refused_while_a_refund_is_in_progress():
    booking = booking_worth("200.00", Status.PAID)
    payment = add_payment(booking, "200.00", method=Payment.Method.CARD)
    add_payment(
        booking,
        "50.00",
        status=Payment.Status.PENDING,
        kind=Payment.Kind.RENTAL_REFUND,
        refund_of=payment,
    )

    with pytest.raises(lifecycle.BookingStateError, match="still in progress"):
        lifecycle.cancel_booking(booking)


@pytest.mark.parametrize("status", [Status.CANCELLED, Status.DELIVERED, Status.RETURNED])
def test_cancel_is_refused_for_finished_bookings(status):
    booking = booking_worth("200.00", status)
    with pytest.raises(lifecycle.BookingStateError):
        lifecycle.cancel_booking(booking)


def test_fully_paid_booking_is_delivered_without_an_override(staff):
    booking = booking_worth("200.00", Status.PAID, caution_fee="75.00")
    add_payment(booking, "200.00", caution="75.00")

    lifecycle.mark_delivered(booking, by=staff)

    booking.refresh_from_db()
    assert booking.status == Status.DELIVERED
    assert not booking.needs_attention
    assert booking.delivery_override_by is None
    assert booking.delivery_override_note == ""


@pytest.mark.parametrize(
    ("paid", "caution", "pending", "reason"),
    [
        ("100.00", "75.00", False, r"\$100.00 of the rental is still owed"),
        ("200.00", "0.00", False, r"\$75.00 of the caution deposit has not been collected"),
        ("200.00", "75.00", True, "a payment or refund is still in progress"),
    ],
)
def test_delivery_is_refused_without_an_override_note(staff, paid, caution, pending, reason):
    status = Status.PAID if paid == "200.00" else Status.DEPOSIT_PAID
    booking = booking_worth("200.00", status, caution_fee="75.00")
    add_payment(booking, paid, caution=caution)
    if pending:
        add_payment(booking, "10.00", status=Payment.Status.PENDING, kind=Payment.Kind.BALANCE)

    for note in ("", "   "):
        with pytest.raises(lifecycle.BookingStateError, match=reason):
            lifecycle.mark_delivered(booking, by=staff, override_note=note)

    booking.refresh_from_db()
    assert booking.status == status


def test_delivery_override_records_the_note_and_who_gave_it(staff):
    booking = booking_worth("200.00", Status.DEPOSIT_PAID)
    add_payment(booking, "100.00")

    lifecycle.mark_delivered(booking, by=staff, override_note="Balance collected on delivery")

    booking.refresh_from_db()
    assert booking.status == Status.DELIVERED
    assert booking.needs_attention
    assert booking.delivery_override_by == staff
    assert booking.delivery_override_at is not None
    assert booking.delivery_override_note.startswith("Balance collected on delivery")
    assert "$100.00 of the rental is still owed" in booking.delivery_override_note


@pytest.mark.parametrize(
    "status", [Status.ENQUIRY, Status.QUOTED, Status.EXPIRED, Status.CANCELLED, Status.RETURNED]
)
def test_only_a_secured_booking_can_be_delivered_even_with_a_note(staff, status):
    booking = booking_worth("200.00", status)
    with pytest.raises(lifecycle.BookingStateError, match="deposit paid or paid in full"):
        lifecycle.mark_delivered(booking, by=staff, override_note="Let it go")


def test_returned_only_follows_delivered_and_releases_stock():
    booking = booking_worth("200.00", Status.PAID, stock=1)
    add_payment(booking, "200.00")
    variant = booking.items.get().variant
    with pytest.raises(lifecycle.BookingStateError):
        lifecycle.mark_returned(booking)

    Booking.objects.filter(pk=booking.pk).update(status=Status.DELIVERED)
    lifecycle.mark_returned(booking)

    booking.refresh_from_db()
    assert booking.status == Status.RETURNED
    assert availability.available_quantity(variant, EVENT_DATE + timedelta(days=30)) == 1
    assert availability.available_quantity(variant, EVENT_DATE) == 1


def test_raising_a_quantity_on_a_paid_booking_drops_it_to_deposit_paid_and_flags():
    booking = booking_worth("100.00", Status.PAID)
    add_payment(booking, "100.00")
    variant = booking.items.get().variant

    lifecycle.change_item(booking, variant, quantity=2)

    booking.refresh_from_db()
    assert booking.total == D("200.00")
    assert booking.balance_due == D("100.00")
    assert booking.status == Status.DEPOSIT_PAID
    assert booking.needs_attention


def test_removing_a_line_from_a_paid_booking_flags_the_overpayment():
    booking = booking_worth("100.00", Status.PAID)
    extra = BookingItemFactory(booking=booking, quantity=1, unit_price=D("50.00"))
    add_payment(booking, "150.00")

    lifecycle.change_item(booking, extra.variant, quantity=0)

    booking.refresh_from_db()
    assert booking.total == D("100.00")
    assert booking.amount_paid == D("150.00")
    assert booking.status == Status.PAID
    assert booking.needs_attention


def test_changing_an_item_beyond_stock_is_refused():
    booking = booking_worth("100.00", Status.PAID, stock=2)
    add_payment(booking, "100.00")
    variant = booking.items.get().variant
    BookingItemFactory(variant=variant, quantity=1, booking__status=Status.PAID)

    with pytest.raises(lifecycle.BookingStateError, match="Not enough stock"):
        lifecycle.change_item(booking, variant, quantity=2)

    booking.refresh_from_db()
    assert booking.items.get().quantity == 1
    assert booking.status == Status.PAID
    assert not booking.needs_attention


def test_changing_an_item_is_refused_while_a_checkout_is_open():
    booking = booking_worth("200.00", Status.DEPOSIT_PAID)
    add_payment(booking, "100.00")
    add_payment(booking, "100.00", status=Payment.Status.PENDING, kind=Payment.Kind.BALANCE)

    with pytest.raises(lifecycle.BookingStateError, match="would charge the old total"):
        lifecycle.change_item(booking, booking.items.get().variant, quantity=3)

    assert booking.items.get().quantity == 1


def test_changing_an_item_without_payments_does_not_flag():
    booking = booking_worth("100.00", Status.QUOTED)

    lifecycle.change_item(booking, booking.items.get().variant, quantity=3, unit_price=D("90.00"))

    booking.refresh_from_db()
    assert booking.total == D("270.00")
    assert not booking.needs_attention
