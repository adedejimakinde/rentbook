"""Booking lifecycle: the only way staff move a booking's status or change its lines.

Every function locks the booking row first and checks the ledger (balance,
payments and refunds in progress, caution) before changing anything, so a
status can never drift away from the money actually on the booking.
"""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.bookings.models import Booking, BookingItem, BookingLogEntry
from apps.bookings.services import availability, quote
from apps.invoices import hooks as documents
from apps.payments.models import Payment
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


def _clearing_ach(entries):
    """The entries that are ACH payments still clearing (and nothing else)."""
    return entries.filter(
        kind__in=Payment.RENTAL_KINDS,
        method=Payment.Method.ACH,
        mismatch_flagged_at__isnull=True,
    )


def _check_or_override(booking, blocking, *, action, by, override_note, refusal):
    """Refuse action while blocking entries are moving, unless only ACH is clearing.

    An ACH payment can take days to clear, so staff may go ahead anyway with
    an override note. The note, the action, who and when are recorded on the
    booking, which is flagged: when the ACH settles, staff refund or collect
    the difference. Card checkouts and refunds in progress cannot be
    overridden. Saves nothing itself; the caller saves the booking.
    """
    if not blocking.exists():
        return
    override_note = (override_note or "").strip()
    only_ach = not blocking.exclude(pk__in=_clearing_ach(blocking)).exists()
    if not only_ach:
        raise BookingStateError(refusal)
    if not override_note:
        raise BookingStateError(
            "An ACH bank payment on this booking is still clearing. Wait for it, or enter "
            "an override note to go ahead anyway."
        )
    now = timezone.now()
    line = f"{timezone.localtime(now):%Y-%m-%d %H:%M} {action} by {by}: {override_note}"
    booking.clearing_override_note = "\n".join(
        part for part in (booking.clearing_override_note, line) if part
    )
    booking.clearing_override_by = by
    booking.clearing_override_at = now
    booking.needs_attention = True


OVERRIDE_FIELDS = ["clearing_override_note", "clearing_override_by", "clearing_override_at"]


def cancel_booking(booking, *, by=None, override_note=""):
    """Cancel a booking and release its stock and its promo code use.

    Refused while a payment or refund is still in progress, except an ACH
    payment still clearing, which staff can override with a note. Money still
    on the booking (rental paid or caution held) flags it, so staff refund it.
    """
    with transaction.atomic():
        booking = ledger.lock_booking(booking.pk)
        if booking.status == Booking.Status.CANCELLED:
            raise BookingStateError("This booking is already cancelled.")
        if booking.status in ledger.FULFILLED_STATUSES:
            raise BookingStateError("This booking has been delivered and cannot be cancelled.")
        _check_or_override(
            booking,
            booking.payments.in_progress(),
            action="Cancelled",
            by=by,
            override_note=override_note,
            refusal=(
                "A payment or refund on this booking is still in progress. "
                "Wait for it to finish before cancelling."
            ),
        )
        booking.status = Booking.Status.CANCELLED
        booking.hold_expires_at = None
        if booking.amount_paid > ZERO or booking.caution_held > ZERO:
            booking.needs_attention = True
        ledger.release_promo(booking)
        booking.save(
            update_fields=[
                "status",
                "hold_expires_at",
                "needs_attention",
                "promo_redeemed_at",
                *OVERRIDE_FIELDS,
                "updated_at",
            ]
        )
    return booking


def reopen_booking(booking):
    """Bring a cancelled booking back, re-holding its stock, or raise BookingStateError.

    The booking is locked first, then its stock is locked and checked again;
    if any of it has been taken since, nothing changes and the shortage is
    reported. The new status follows the money still on the booking: paid in
    full, deposit paid, or otherwise a staff hold (flagged if some money is
    on it). A promo use given back on cancelling is taken again when money is
    on the booking.
    """
    with transaction.atomic():
        booking = ledger.lock_booking(booking.pk)
        if booking.status != Booking.Status.CANCELLED:
            raise BookingStateError("Only a cancelled booking can be reopened.")
        if booking.event_date < timezone.localdate():
            raise BookingStateError("This booking's event date has already passed.")
        if booking.payments.in_progress().exists():
            raise BookingStateError(
                "A payment or refund on this booking is still in progress. "
                "Wait for it to finish before reopening."
            )
        quantities = dict(booking.items.values_list("variant_id", "quantity"))
        if quantities:
            try:
                availability.lock_and_check(quantities, booking.event_date, exclude_booking=booking)
            except availability.InsufficientStockError as error:
                raise BookingStateError(f"Cannot reopen. {error}") from error

        paid, total, deposit = booking.amount_paid, booking.total, booking.deposit_amount
        if paid > ZERO and paid >= total:
            booking.status = Booking.Status.PAID
        elif paid > ZERO and paid >= deposit:
            booking.status = Booking.Status.DEPOSIT_PAID
        else:
            booking.status = Booking.Status.HELD
            booking.hold_expires_at = availability.staff_hold_expiry()
            booking.needs_attention |= paid > ZERO
        if booking.status != Booking.Status.HELD:
            booking.hold_expires_at = None
        if paid > ZERO:
            ledger.redeem_promo(booking)
        booking.save(
            update_fields=[
                "status",
                "hold_expires_at",
                "needs_attention",
                "promo_redeemed_at",
                "updated_at",
            ]
        )
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


def change_item(booking, variant, *, quantity, unit_price=None, by=None, override_note=""):
    """Set how many units of a variant a booking has; zero removes the line.

    The way to change lines once a booking has payments. Stock is locked and
    checked, and an open checkout (which would charge the old total) refuses
    the change; an ACH payment still clearing can be overridden with a note.
    Tax and the caution owed are then recomputed from the new lines. More
    caution owed is collected with the balance, as its own checkout line; less
    leaves caution to refund. The booking is re-checked against its payments
    and flagged so staff collect or refund the difference.
    """
    if quantity < 0:
        raise BookingStateError("Quantity cannot be negative.")
    if unit_price is not None and unit_price < ZERO:
        raise BookingStateError("Unit price cannot be negative.")
    with transaction.atomic():
        booking = ledger.lock_booking(booking.pk)
        if booking.status in (Booking.Status.CANCELLED, *ledger.FULFILLED_STATUSES):
            raise BookingStateError("Items cannot be changed on a booking in this status.")
        _check_or_override(
            booking,
            booking.payments.open_checkouts(),
            action="Changed items",
            by=by,
            override_note=override_note,
            refusal=(
                "A card or bank payment for this booking is in progress and would charge "
                "the old total. Wait for it to finish before changing items."
            ),
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
        _reprice(booking, extra_fields=OVERRIDE_FIELDS + ["needs_attention"])
        ledger.reassess_after_change(booking)
        documents.booking_changed(booking)
    return booking


def _reprice(booking, extra_fields=()):
    """Recompute a locked booking's tax and caution owed from its lines, and save them.

    Caution held beyond the new amount owed flags the booking for a refund.
    """
    booking.tax = quote.booking_tax(booking)
    booking.caution_fee = quote.booking_caution(booking)
    if booking.caution_excess > ZERO:
        booking.needs_attention = True
    booking.save(
        update_fields=["tax", "caution_fee", "needs_attention", *extra_fields, "updated_at"]
    )


def set_tax_exemption(booking, *, exempt, certificate, by):
    """Mark a booking tax exempt (with its certificate) or taxable again, and reprice it.

    Needs the "can set tax exemption" permission, which the admin checks.
    Refused while a checkout is open, since it would charge the old total.
    Who made the change and when are recorded; tax is recomputed (zero when
    exempt) and the booking is re-checked against its payments.
    """
    certificate = (certificate or "").strip()
    if exempt and not certificate:
        raise BookingStateError("Enter the exemption certificate to mark the booking tax exempt.")
    with transaction.atomic():
        booking = ledger.lock_booking(booking.pk)
        if booking.status in (Booking.Status.CANCELLED, *ledger.FULFILLED_STATUSES):
            raise BookingStateError("Tax cannot be changed on a booking in this status.")
        if booking.payments.open_checkouts().exists():
            raise BookingStateError(
                "A card or bank payment for this booking is in progress and would charge "
                "the old total. Wait for it to finish before changing its tax."
            )
        booking.tax_exempt = exempt
        booking.tax_exempt_certificate = certificate if exempt else ""
        booking.tax_exempt_by = by
        booking.tax_exempt_at = timezone.now()
        booking.tax = quote.booking_tax(booking)
        booking.save(
            update_fields=[
                "tax_exempt",
                "tax_exempt_certificate",
                "tax_exempt_by",
                "tax_exempt_at",
                "tax",
                "updated_at",
            ]
        )
        ledger.reassess_after_change(booking)
        documents.booking_changed(booking)
    return booking


REFUND_KINDS = (Payment.Kind.RENTAL_REFUND, Payment.Kind.CAUTION_REFUND)


def attention_blockers(booking):
    """Problems behind a flagged booking that are still open, where the data can tell."""
    blockers = []
    payments = booking.payments.all()
    if payments.filter(status=Payment.Status.PENDING, mismatch_flagged_at__isnull=False).exists():
        blockers.append("a payment Stripe charged a different amount for is still unresolved")
    if payments.filter(status=Payment.Status.PENDING, kind__in=REFUND_KINDS).exists():
        blockers.append("a refund is still pending")
    if booking.status in availability.STOCK_HOLDING_STATUSES:
        variant_ids = list(booking.items.values_list("variant_id", flat=True))
        short = availability.bookings_left_short(variant_ids)
        if any(other.pk == booking.pk for other in short):
            blockers.append("stock is still short for this date")
    return blockers


def resolve_attention(booking, note, by):
    """Clear a booking's needs-attention flag, recording the note, who and when in its log.

    Refused without a note, when the booking is not flagged, or while a payment
    mismatch, a refund in progress or a stock shortage behind the flag is still
    open.
    """
    note = (note or "").strip()
    if not note:
        raise BookingStateError("Add a note saying how this was resolved.")
    with transaction.atomic():
        booking = ledger.lock_booking(booking.pk)
        if not booking.needs_attention:
            raise BookingStateError("This booking is not flagged as needing attention.")
        blockers = attention_blockers(booking)
        if blockers:
            raise BookingStateError("Cannot resolve yet: " + "; ".join(blockers) + ".")
        booking.needs_attention = False
        booking.save(update_fields=["needs_attention", "updated_at"])
        BookingLogEntry.objects.create(
            booking=booking, action=BookingLogEntry.RESOLVED_ATTENTION, note=note, by=by
        )
    return booking
