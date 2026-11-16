"""Booking lifecycle: the only way staff move a booking's status or change its lines.

Every function locks the booking row first and checks the ledger (balance,
payments and refunds in progress, caution) before changing anything, so a
status can never drift away from the money actually on the booking.
"""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.bookings.models import Booking, BookingItem
from apps.bookings.services import availability
from apps.payments.services import ledger
from apps.payments.services.ledger import PaymentError

ZERO = Decimal("0.00")


class BookingStateError(PaymentError):
    """Raised when a booking cannot be moved or changed as requested."""


def holds_stock(booking, now=None):
    """True when the booking currently ties up stock."""
    if booking.status == Booking.Status.HELD:
        return ledger.has_live_hold(booking, now)
    return booking.status in availability.STOCK_HOLDING_STATUSES


def cancel_booking(booking):
    """Cancel a booking and release its stock.

    Refused while a payment or refund is still in progress. Money still on the
    booking (rental paid or caution held) flags it, so staff refund it.
    """
    with transaction.atomic():
        booking = ledger.lock_booking(booking.pk)
        if booking.status == Booking.Status.CANCELLED:
            raise BookingStateError("This booking is already cancelled.")
        if booking.status in ledger.FULFILLED_STATUSES:
            raise BookingStateError("This booking has been delivered and cannot be cancelled.")
        if booking.payments.in_progress().exists():
            raise BookingStateError(
                "A payment or refund on this booking is still in progress. "
                "Wait for it to finish before cancelling."
            )
        booking.status = Booking.Status.CANCELLED
        booking.hold_expires_at = None
        if booking.amount_paid > ZERO or booking.caution_held > ZERO:
            booking.needs_attention = True
        booking.save(update_fields=["status", "hold_expires_at", "needs_attention", "updated_at"])
    return booking


def delivery_blockers(booking):
    """Reasons a booking should not go out yet: money owed or still moving."""
    blockers = []
    balance = booking.balance_due
    if balance > ZERO:
        blockers.append(f"${balance} of the rental is still owed")
    if booking.payments.in_progress().exists():
        blockers.append("a payment or refund is still in progress")
    caution_due = booking.caution_fee - booking.payments.caution_collected()
    if caution_due > ZERO:
        blockers.append(f"${caution_due} of the caution deposit has not been collected")
    return blockers


def mark_delivered(booking, *, by, override_note=""):
    """Mark a secured booking delivered.

    Needs a zero balance, the caution deposit collected and nothing in
    progress. Staff can deliver anyway by giving override_note (for example
    "balance collected on delivery"); the note, who gave it and when are
    recorded and the booking is flagged.
    """
    override_note = (override_note or "").strip()
    with transaction.atomic():
        booking = ledger.lock_booking(booking.pk)
        if booking.status not in ledger.SECURED_STATUSES:
            raise BookingStateError(
                "Only a booking with its deposit paid or paid in full can be marked delivered."
            )
        blockers = delivery_blockers(booking)
        if blockers and not override_note:
            raise BookingStateError(
                "Cannot mark delivered: " + "; ".join(blockers) + ". "
                "Enter an override note to deliver anyway."
            )
        if blockers:
            booking.delivery_override_note = f"{override_note} (Overrode: {'; '.join(blockers)}.)"
            booking.delivery_override_by = by
            booking.delivery_override_at = timezone.now()
            booking.needs_attention = True
        booking.status = Booking.Status.DELIVERED
        booking.save(
            update_fields=[
                "status",
                "needs_attention",
                "delivery_override_note",
                "delivery_override_by",
                "delivery_override_at",
                "updated_at",
            ]
        )
    return booking


def mark_returned(booking):
    """Mark a delivered booking returned, releasing its stock.

    Caution money still held stays on the ledger for the refund and damage
    charge actions.
    """
    with transaction.atomic():
        booking = ledger.lock_booking(booking.pk)
        if booking.status != Booking.Status.DELIVERED:
            raise BookingStateError("Only a delivered booking can be marked returned.")
        booking.status = Booking.Status.RETURNED
        booking.save(update_fields=["status", "updated_at"])
    return booking


def change_item(booking, variant, *, quantity, unit_price=None):
    """Set how many units of a variant a booking has; zero removes the line.

    The way to change lines once a booking has payments. Stock is locked and
    checked, an open checkout (which would charge the old total) refuses the
    change, and the booking is then re-checked against its payments and
    flagged so staff collect or refund the difference.
    """
    if quantity < 0:
        raise BookingStateError("Quantity cannot be negative.")
    if unit_price is not None and unit_price < ZERO:
        raise BookingStateError("Unit price cannot be negative.")
    with transaction.atomic():
        booking = ledger.lock_booking(booking.pk)
        if booking.status in (Booking.Status.CANCELLED, *ledger.FULFILLED_STATUSES):
            raise BookingStateError("Items cannot be changed on a booking in this status.")
        if booking.payments.open_checkouts().exists():
            raise BookingStateError(
                "A card or bank payment for this booking is in progress and would charge "
                "the old total. Wait for it to finish before changing items."
            )
        line = booking.items.select_for_update().filter(variant=variant).first()
        if line is None and quantity == 0:
            raise BookingStateError("This booking has no line for that item.")
        if line is not None and quantity < line.package_quantity:
            raise BookingStateError(
                f"{line.package_quantity} of these belong to the booking's packages."
            )
        if quantity and holds_stock(booking):
            try:
                availability.lock_and_check(
                    {variant.pk: quantity}, booking.event_date, exclude_booking=booking
                )
            except availability.InsufficientStockError as error:
                raise BookingStateError(str(error)) from error

        if quantity == 0:
            line.delete()
        elif line is None:
            price = variant.unit_price if unit_price is None else unit_price
            BookingItem.objects.create(
                booking=booking, variant=variant, quantity=quantity, unit_price=price
            )
        else:
            line.quantity = quantity
            if unit_price is not None:
                line.unit_price = unit_price
            line.save(update_fields=["quantity", "unit_price", "updated_at"])
        ledger.reassess_after_change(booking)
    return booking
