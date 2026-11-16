"""The ledger: the only place a payment changes a booking.

Every function here locks the booking row before reading its money totals, so
two payments landing at once cannot both act on a stale balance. Lock order is
always booking first, then payment.
"""

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.services import availability
from apps.payments.models import Payment

ZERO = Decimal("0.00")
CENT = Decimal("0.01")

REOPENABLE_STATUSES = (Booking.Status.ENQUIRY, Booking.Status.QUOTED, Booking.Status.EXPIRED)
SECURED_STATUSES = (Booking.Status.DEPOSIT_PAID, Booking.Status.PAID)
FULFILLED_STATUSES = (Booking.Status.DELIVERED, Booking.Status.RETURNED)


class PaymentError(Exception):
    """Raised when a payment cannot be taken or recorded as requested."""


def lock_booking(booking_id):
    """Return the booking with its row locked. Must run inside transaction.atomic()."""
    return Booking.objects.select_for_update().get(pk=booking_id)


def has_live_hold(booking, now=None):
    """True when the booking is held and its hold has not lapsed."""
    now = now or timezone.now()
    return booking.status == Booking.Status.HELD and booking.hold_expires_at > now


def _lock_payment(payment_id):
    """Lock the payment's booking, then the payment, and return both."""
    booking_id = Payment.objects.values_list("booking_id", flat=True).get(pk=payment_id)
    booking = lock_booking(booking_id)
    payment = Payment.objects.select_for_update().get(pk=payment_id)
    return booking, payment


def _stock_still_free(booking):
    """True when the booking's items can still be supplied on its date."""
    quantities = dict(booking.items.values_list("variant_id", "quantity"))
    if not quantities:
        return True
    try:
        availability.lock_and_check(quantities, booking.event_date, exclude_booking=booking)
    except availability.InsufficientStockError:
        return False
    return True


def _apply_money_in(booking):
    """Move a locked booking forward after rental money arrived.

    Money that cannot be matched to a clean status change (stock gone, booking
    cancelled, under the deposit, overpaid) is still kept on the ledger and the
    booking is flagged for staff instead.
    """
    paid, total, deposit = booking.amount_paid, booking.total, booking.deposit_amount
    status = booking.status

    if status in FULFILLED_STATUSES:
        can_progress = False
    elif status in SECURED_STATUSES or has_live_hold(booking):
        can_progress = True
    elif status in REOPENABLE_STATUSES or status == Booking.Status.HELD:
        can_progress = _stock_still_free(booking)
        booking.needs_attention |= not can_progress
    else:
        can_progress = False
        booking.needs_attention = True

    if paid > total:
        booking.needs_attention = True
    if can_progress:
        if paid >= total:
            booking.status = Booking.Status.PAID
        elif paid >= deposit:
            booking.status = Booking.Status.DEPOSIT_PAID
        else:
            booking.needs_attention = True
        if booking.status in SECURED_STATUSES:
            booking.hold_expires_at = None
    booking.save(update_fields=["status", "hold_expires_at", "needs_attention", "updated_at"])


def _apply_money_out(booking):
    """Re-evaluate a locked booking after rental money was refunded."""
    paid, total, deposit = booking.amount_paid, booking.total, booking.deposit_amount
    if booking.status in FULFILLED_STATUSES:
        booking.needs_attention |= paid < total
    elif booking.status in (Booking.Status.HELD, *SECURED_STATUSES):
        if paid == ZERO:
            booking.status = Booking.Status.CANCELLED
        elif paid < deposit:
            booking.needs_attention = True
        elif paid < total:
            booking.status = Booking.Status.DEPOSIT_PAID
            booking.hold_expires_at = None
    booking.save(update_fields=["status", "hold_expires_at", "needs_attention", "updated_at"])


def _mark_succeeded(payment, payment_intent_id=None, method=None):
    payment.status = Payment.Status.SUCCEEDED
    payment.received_at = payment.received_at or timezone.now()
    if payment_intent_id:
        payment.stripe_payment_intent_id = payment_intent_id
    if method:
        payment.method = method
    payment.save()


def record_success(payment_id, *, payment_intent_id=None, method=None):
    """Mark a payment succeeded and update its booking. Repeating it changes nothing.

    A payment previously marked failed is still accepted: if Stripe says the
    money arrived, it is recorded.
    """
    with transaction.atomic():
        booking, payment = _lock_payment(payment_id)
        if payment.status in (Payment.Status.SUCCEEDED, Payment.Status.REFUNDED):
            return payment
        _mark_succeeded(payment, payment_intent_id, method)
        _apply_money_in(booking)
    return payment


def record_processing(payment_id, *, payment_intent_id=None):
    """Note that an ACH payment is clearing, and keep the stock held meanwhile."""
    with transaction.atomic():
        booking, payment = _lock_payment(payment_id)
        if payment.status != Payment.Status.PENDING:
            return payment
        payment.method = Payment.Method.ACH
        if payment_intent_id:
            payment.stripe_payment_intent_id = payment_intent_id
        payment.save()
        if booking.status == Booking.Status.HELD:
            clearing_until = timezone.now() + timedelta(days=settings.ACH_HOLD_DAYS)
            booking.hold_expires_at = max(booking.hold_expires_at, clearing_until)
            booking.save(update_fields=["hold_expires_at", "updated_at"])
    return payment


def record_failure(payment_id, *, method=None):
    """Mark a pending payment failed. The booking's status and totals do not change.

    A failed ACH payment flags the booking, since its stock was kept on hold
    while the payment was clearing.
    """
    with transaction.atomic():
        booking, payment = _lock_payment(payment_id)
        if payment.status != Payment.Status.PENDING:
            return payment
        payment.status = Payment.Status.FAILED
        payment.method = method or payment.method
        payment.save()
        if payment.method == Payment.Method.ACH:
            flag_attention(booking)
    return payment


def record_refund(payment_id, refunded_total, *, ambiguous=False):
    """Set the cumulative rental amount refunded on a payment and update its booking.

    refunded_total is a running total, so a repeated or out-of-date report
    changes nothing. It counts against the rental amount first, then against
    any caution taken in the same payment. ambiguous marks refunds made outside
    the app (the Stripe dashboard), where rental and caution cannot be told
    apart: a partial one on a payment that carried caution flags the booking.
    """
    with transaction.atomic():
        booking, payment = _lock_payment(payment_id)
        if payment.status in (Payment.Status.PENDING, Payment.Status.FAILED):
            _mark_succeeded(payment)
            _apply_money_in(booking)
        refunded_total = min(refunded_total, payment.charged_total)
        if refunded_total <= payment.refunded_amount:
            return payment
        payment.refunded_amount = refunded_total
        if refunded_total >= payment.amount:
            payment.status = Payment.Status.REFUNDED
        payment.save()
        partial = refunded_total < payment.charged_total
        if ambiguous and payment.caution_amount > ZERO and partial:
            booking.needs_attention = True
        _apply_money_out(booking)
    return payment


def record_stripe_caution_refund(payment_id, refund_id, amount, *, received_by=None):
    """Record a caution refund Stripe sent back against a payment, once per refund id.

    The refund has already happened at Stripe, so it is always recorded; if it
    takes the caution held below zero the booking is flagged.
    """
    with transaction.atomic():
        booking, payment = _lock_payment(payment_id)
        existing = Payment.objects.filter(stripe_refund_id=refund_id).first()
        if existing is not None:
            return existing
        refund = Payment.objects.create(
            booking=booking,
            amount=amount,
            method=payment.method,
            kind=Payment.Kind.CAUTION_REFUND,
            status=Payment.Status.SUCCEEDED,
            stripe_refund_id=refund_id,
            refund_of=payment,
            reference=refund_id,
            received_by=received_by,
            received_at=timezone.now(),
        )
        if booking.caution_held < ZERO:
            flag_attention(booking)
    return refund


def check_caution_refund(booking_id, amount):
    """Return the Stripe payment a caution refund of amount can be sent back against.

    Raises PaymentError when the amount is more than the caution held, or no
    single card or ACH payment holds that much caution.
    """
    amount = positive_amount(amount)
    with transaction.atomic():
        booking = lock_booking(booking_id)
        held = booking.caution_held
        if amount > held:
            raise PaymentError(f"Amount is more than the caution deposit still held (${held}).")
        for payment in booking.payments.filter(
            kind__in=Payment.RENTAL_KINDS, stripe_payment_intent_id__isnull=False
        ):
            if payment.caution_refundable >= amount:
                return payment
    raise PaymentError(
        "No card or ACH payment holds that much caution. Refund it by Zelle, Cash App "
        "or bank deposit and record it that way."
    )


def check_rental_refund(payment_id, amount):
    """Validate a rental refund of amount against a Stripe payment and return the payment."""
    amount = positive_amount(amount)
    with transaction.atomic():
        _, payment = _lock_payment(payment_id)
        if payment.kind not in Payment.RENTAL_KINDS or not payment.stripe_payment_intent_id:
            raise PaymentError("Only card and ACH rental payments can be refunded here.")
        if payment.status not in (Payment.Status.SUCCEEDED, Payment.Status.REFUNDED):
            raise PaymentError("This payment has not succeeded, so there is nothing to refund.")
        if amount > payment.rental_net:
            raise PaymentError(
                f"Amount is more than the rental money left on this payment "
                f"(${payment.rental_net})."
            )
    return payment


def record_manual_rental_refund(payment, *, amount, method, reference, received_at, received_by):
    """Record rental money staff sent back by Zelle, Cash App or bank deposit.

    Only for a manual payment; card and ACH payments are refunded through
    Stripe. The refund cannot exceed the rental money left on the payment, and
    the booking is re-evaluated exactly as for a Stripe refund.
    """
    if method not in Payment.MANUAL_METHODS:
        raise PaymentError("Manual refunds must be by Zelle, Cash App or bank deposit.")
    if not reference:
        raise PaymentError("Enter the refund reference.")
    amount = positive_amount(amount)
    with transaction.atomic():
        booking, payment = _lock_payment(payment.pk)
        if (
            payment.kind not in Payment.RENTAL_KINDS
            or payment.method not in Payment.MANUAL_METHODS
            or payment.stripe_payment_intent_id
        ):
            raise PaymentError("Card and ACH payments are refunded through Stripe.")
        if payment.status not in (Payment.Status.SUCCEEDED, Payment.Status.REFUNDED):
            raise PaymentError("This payment has not succeeded, so there is nothing to refund.")
        if amount > payment.rental_net:
            raise PaymentError(
                f"Amount is more than the rental money left on this payment "
                f"(${payment.rental_net})."
            )
        entry = Payment.objects.create(
            booking=booking,
            amount=amount,
            method=method,
            kind=Payment.Kind.RENTAL_REFUND,
            status=Payment.Status.SUCCEEDED,
            refund_of=payment,
            reference=reference,
            received_by=received_by,
            received_at=received_at,
        )
        record_refund(payment.pk, payment.refunded_amount + amount)
    return entry


def flag_attention(booking):
    """Flag a booking for staff follow-up."""
    booking.needs_attention = True
    booking.save(update_fields=["needs_attention", "updated_at"])


def _clean_amount(value, label):
    amount = Decimal(value)
    if amount < ZERO or amount != amount.quantize(CENT):
        raise PaymentError(f"{label} must be a dollar amount of zero or more.")
    return amount


def positive_amount(value):
    """Return value as a dollar Decimal, or raise PaymentError unless it is above zero."""
    amount = _clean_amount(value, "Amount")
    if amount <= ZERO:
        raise PaymentError("Enter an amount greater than zero.")
    return amount


def record_manual_payment(
    booking,
    *,
    method,
    amount,
    received_at,
    reference,
    received_by,
    caution_amount=ZERO,
):
    """Record a Zelle, Cash App or bank deposit payment received by staff.

    Updates the booking exactly as a Stripe payment would. Rejects amounts
    above the balance due, and caution money above what is still to collect.
    """
    if method not in Payment.MANUAL_METHODS:
        raise PaymentError("Manual payments must be Zelle, Cash App or bank deposit.")
    amount = _clean_amount(amount, "Amount")
    caution_amount = _clean_amount(caution_amount, "Caution amount")
    if amount + caution_amount <= ZERO:
        raise PaymentError("Enter an amount greater than zero.")

    with transaction.atomic():
        booking = lock_booking(booking.pk)
        if booking.status == Booking.Status.CANCELLED:
            raise PaymentError("This booking is cancelled. Reopen it before recording a payment.")
        paid, balance = booking.amount_paid, booking.balance_due
        if amount > balance:
            raise PaymentError(f"Amount is more than the balance due (${balance}).")
        caution_due = booking.caution_fee - booking.payments.caution_collected()
        if caution_amount > caution_due:
            raise PaymentError(
                f"Caution amount is more than the caution still to collect (${caution_due})."
            )
        if paid > ZERO:
            kind = Payment.Kind.BALANCE
        elif amount == balance:
            kind = Payment.Kind.FULL
        else:
            kind = Payment.Kind.DEPOSIT
        payment = Payment.objects.create(
            booking=booking,
            amount=amount,
            caution_amount=caution_amount,
            method=method,
            kind=kind,
            status=Payment.Status.SUCCEEDED,
            reference=reference,
            received_by=received_by,
            received_at=received_at,
        )
        if amount > ZERO:
            _apply_money_in(booking)
    return payment


def _release_caution(booking, kind, *, amount, method, reference, received_at, received_by):
    amount = positive_amount(amount)
    with transaction.atomic():
        booking = lock_booking(booking.pk)
        held = booking.caution_held
        if amount > held:
            raise PaymentError(f"Amount is more than the caution deposit still held (${held}).")
        return Payment.objects.create(
            booking=booking,
            amount=amount,
            method=method,
            kind=kind,
            status=Payment.Status.SUCCEEDED,
            reference=reference,
            received_by=received_by,
            received_at=received_at,
        )


def record_caution_refund(booking, *, amount, method, reference, received_at, received_by):
    """Record caution deposit money staff returned by Zelle, Cash App or bank deposit.

    Refunds back to a card or bank account go through
    services.stripe.refund_caution instead, which moves the money itself.
    """
    if method not in Payment.MANUAL_METHODS:
        raise PaymentError("Manual caution refunds must be Zelle, Cash App or bank deposit.")
    return _release_caution(
        booking,
        Payment.Kind.CAUTION_REFUND,
        amount=amount,
        method=method,
        reference=reference,
        received_at=received_at,
        received_by=received_by,
    )


def record_damage_charge(booking, *, amount, reference, received_at, received_by):
    """Record caution deposit money kept to cover damage."""
    return _release_caution(
        booking,
        Payment.Kind.DAMAGE_CHARGE,
        amount=amount,
        method="",
        reference=reference,
        received_at=received_at,
        received_by=received_by,
    )
