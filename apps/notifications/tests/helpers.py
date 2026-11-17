"""Bookings in the states reminders and texts look at."""

from datetime import datetime, timedelta
from decimal import Decimal

from django.utils import timezone

from apps.bookings.models import Booking, PaymentChoice
from apps.notifications.models import Notification
from apps.payments.models import Payment
from apps.payments.services import ledger
from apps.payments.tests.helpers import held_booking

CUSTOMER_PHONE = "+14695550101"


def at(day, hour=12, minute=0):
    """An aware datetime on day at hour:minute business time."""
    return timezone.make_aware(
        datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
    )


def manual_booking(*, consent=True, hold_hours=24, **kwargs):
    """A held booking waiting for a Zelle payment."""
    kwargs.setdefault("payment_choice", PaymentChoice.ZELLE)
    booking = held_booking(total="200.00", phone="(469) 555-0101", sms_consent=consent, **kwargs)
    booking.hold_expires_at = timezone.now() + timedelta(hours=hold_hours)
    booking.save(update_fields=["hold_expires_at"])
    return booking


def paid_booking(amount, *, consent=True, **kwargs):
    """A $200 booking with amount paid by Zelle (100 is the deposit, 200 is in full)."""
    booking = manual_booking(consent=consent, **kwargs)
    ledger.record_manual_payment(
        booking,
        method=Payment.Method.ZELLE,
        amount=Decimal(amount),
        received_at=timezone.now(),
        reference=f"ZL-{booking.pk}",
        received_by=None,
    )
    booking.refresh_from_db()
    return booking


def deposit_booking(**kwargs):
    booking = paid_booking("100.00", **kwargs)
    assert booking.status == Booking.Status.DEPOSIT_PAID
    return booking


def full_booking(**kwargs):
    booking = paid_booking("200.00", **kwargs)
    assert booking.status == Booking.Status.PAID
    return booking


def rows(booking=None, **filters):
    query = Notification.objects.filter(**filters)
    if booking is not None:
        query = query.filter(booking=booking)
    return list(query.order_by("pk"))
