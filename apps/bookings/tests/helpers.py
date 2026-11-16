"""Shared helpers for booking tests: bookings with a price and payments on them."""

from decimal import Decimal

from apps.bookings.tests.factories import BookingFactory, BookingItemFactory, ItemVariantFactory
from apps.payments.models import Payment

D = Decimal


def booking_worth(total, status, caution_fee="0.00", stock=10, **kwargs):
    """A booking with one line of one unit priced at total."""
    booking = BookingFactory(status=status, caution_fee=D(caution_fee), **kwargs)
    variant = ItemVariantFactory(stock_quantity=stock)
    BookingItemFactory(booking=booking, variant=variant, quantity=1, unit_price=D(total))
    return booking


def add_payment(booking, amount, caution="0.00", status=Payment.Status.SUCCEEDED, **kwargs):
    """Put a payment entry straight onto the booking's ledger."""
    return Payment.objects.create(
        booking=booking,
        amount=D(amount),
        caution_amount=D(caution),
        kind=kwargs.pop("kind", Payment.Kind.DEPOSIT),
        method=kwargs.pop("method", Payment.Method.ZELLE),
        status=status,
        **kwargs,
    )
