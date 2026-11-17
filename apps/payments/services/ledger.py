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

from apps.bookings.models import Booking, PromoCode
from apps.bookings.services import availability
from apps.invoices import hooks as documents
from apps.notifications import hooks as notify
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


def promo_is_spent(booking):
    """True when the booking's promo code ran out of uses before its first payment."""
    if booking.promo_code_id is None or booking.promo_redeemed_at is not None:
        return False
    return booking.promo_code.is_used_up


def check_promo_available(booking):
    """Refuse a first payment on a booking whose promo code has run out of uses."""
    if promo_is_spent(booking):
        raise PaymentError(
            "The promo code on this booking has been used the maximum number of times. "
            "Get a new quote without it before paying."
        )


def _redeem_promo(booking):
    """Count the booking's promo code as used, once, on its first successful payment.

    The promo row is locked so two bookings paying at once cannot both take the
    last use. If the code ran out in the meantime the money is still kept and
    the booking is flagged; uses_count never goes above max_uses.
    """
    if booking.promo_code_id is None or booking.promo_redeemed_at is not None:
        return
    promo = PromoCode.objects.select_for_update().get(pk=booking.promo_code_id)
    if promo.is_used_up:
        booking.needs_attention = True
    else:
        promo.uses_count += 1
        promo.save(update_fields=["uses_count", "updated_at"])
    booking.promo_redeemed_at = timezone.now()


def release_promo(booking):
    """Give back the promo use a locked booking took, once, when it is cancelled or refunded.

    Saves nothing on the booking itself: the caller saves promo_redeemed_at.
    """
    if booking.promo_code_id is None or booking.promo_redeemed_at is None:
        return
    promo = PromoCode.objects.select_for_update().get(pk=booking.promo_code_id)
    if promo.uses_count > 0:
        promo.uses_count -= 1
        promo.save(update_fields=["uses_count", "updated_at"])
    booking.promo_redeemed_at = None


def redeem_promo(booking):
    """Count the promo use again on a reopened booking that still has money on it."""
    _redeem_promo(booking)


def _money_in(booking, payment):
    """Apply rental money that arrived on a locked booking, and queue its receipt.

    The receipt goes out as the booking confirmation when this payment is the
    one that secured the booking.
    """
    was_secured = booking.status in (*SECURED_STATUSES, *FULFILLED_STATUSES)
    _apply_money_in(booking)
    confirmed = not was_secured and booking.status in SECURED_STATUSES
    documents.payment_succeeded(payment, confirmed=confirmed)


def _apply_money_in(booking):
    """Move a locked booking forward after rental money arrived.

    Money that cannot be matched to a clean status change (stock gone, booking
    cancelled, under the deposit, overpaid) is still kept on the ledger and the
    booking is flagged for staff instead.
    """
    _redeem_promo(booking)
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
    booking.save(
        update_fields=[
            "status",
            "hold_expires_at",
            "needs_attention",
            "promo_redeemed_at",
            "updated_at",
        ]
    )


def _apply_money_out(booking):
    """Re-evaluate a locked booking after rental money was refunded."""
    paid, total, deposit = booking.amount_paid, booking.total, booking.deposit_amount
    if booking.status in FULFILLED_STATUSES:
        booking.needs_attention |= paid < total
    elif booking.status in (Booking.Status.HELD, *SECURED_STATUSES):
        if paid == ZERO:
            booking.status = Booking.Status.CANCELLED
            booking.hold_expires_at = None
            booking.needs_attention |= booking.caution_held > ZERO
        elif paid < deposit:
            booking.needs_attention = True
        elif paid < total:
            booking.status = Booking.Status.DEPOSIT_PAID
            booking.hold_expires_at = None
    if paid == ZERO:
        release_promo(booking)
    booking.save(
        update_fields=[
            "status",
            "hold_expires_at",
            "needs_attention",
            "promo_redeemed_at",
            "updated_at",
        ]
    )


def _apply_refund_reversal(booking):
    """Re-evaluate a locked booking after a refund it counted failed at Stripe.

    The money is back on the booking but the customer was told it was sent,
    so the booking is always flagged. A secured booking's status follows the
    money again; a booking the refund cancelled stays cancelled for staff to
    reopen. A promo use given back when the money went out is taken again
    now that money is on the booking.
    """
    paid, total, deposit = booking.amount_paid, booking.total, booking.deposit_amount
    if booking.status in SECURED_STATUSES:
        if paid >= total:
            booking.status = Booking.Status.PAID
        elif paid >= deposit:
            booking.status = Booking.Status.DEPOSIT_PAID
    if paid > ZERO:
        _redeem_promo(booking)
    booking.needs_attention = True
    booking.save(update_fields=["status", "needs_attention", "promo_redeemed_at", "updated_at"])


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
    money arrived, it is recorded. The exception is a payment staff failed and
    refunded because Stripe charged the wrong amount.
    """
    with transaction.atomic():
        booking, payment = _lock_payment(payment_id)
        if payment.status in (Payment.Status.SUCCEEDED, Payment.Status.REFUNDED):
            return payment
        if payment.failure_reason == Payment.FailureReason.AMOUNT_MISMATCH:
            return payment
        _mark_succeeded(payment, payment_intent_id, method)
        _money_in(booking, payment)
    return payment


def record_processing(payment_id, *, payment_intent_id=None):
    """Note that an ACH payment is clearing, and keep the stock held meanwhile.

    A live hold is extended. A hold that already lapsed has released its
    stock, so the stock is locked and checked again: the booking goes back on
    hold if it is still free, and is flagged for staff if it is gone.
    """
    with transaction.atomic():
        booking, payment = _lock_payment(payment_id)
        if payment.status != Payment.Status.PENDING:
            return payment
        payment.method = Payment.Method.ACH
        if payment_intent_id:
            payment.stripe_payment_intent_id = payment_intent_id
        payment.save()
        _hold_while_clearing(booking)
        notify.ach_processing(payment)
    return payment


def _hold_while_clearing(booking):
    """Keep a locked booking's stock for the days an ACH payment takes to clear."""
    clearing_until = timezone.now() + timedelta(days=settings.ACH_HOLD_DAYS)
    if has_live_hold(booking):
        booking.hold_expires_at = max(booking.hold_expires_at, clearing_until)
    elif booking.status in (Booking.Status.HELD, *REOPENABLE_STATUSES):
        if _stock_still_free(booking):
            booking.status = Booking.Status.HELD
            booking.hold_expires_at = clearing_until
        else:
            booking.needs_attention = True
    elif booking.status not in SECURED_STATUSES:
        booking.needs_attention = True
    booking.save(update_fields=["status", "hold_expires_at", "needs_attention", "updated_at"])


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


def record_refund(payment_id, refunded_total, *, ambiguous=False, allow_decrease=False):
    """Set the cumulative rental amount refunded on a payment and update its booking.

    refunded_total is a running total, so a repeated or out-of-date report
    changes nothing. It counts against the rental amount first, then against
    any caution taken in the same payment. ambiguous marks refunds made outside
    the app (the Stripe dashboard), where rental and caution cannot be told
    apart: a partial one on a payment that carried caution flags the booking.

    allow_decrease lets the total go down; only for a total Stripe reported
    after a refund already counted failed. The money is then back on the
    booking, which is flagged and re-evaluated.
    """
    with transaction.atomic():
        booking, payment = _lock_payment(payment_id)
        if payment.failure_reason == Payment.FailureReason.AMOUNT_MISMATCH:
            return payment
        if payment.status in (Payment.Status.PENDING, Payment.Status.FAILED):
            _mark_succeeded(payment)
            _money_in(booking, payment)
        refunded_total = max(min(refunded_total, payment.charged_total), ZERO)
        if refunded_total < payment.refunded_amount and allow_decrease:
            payment.refunded_amount = refunded_total
            if payment.status == Payment.Status.REFUNDED and refunded_total < payment.amount:
                payment.status = Payment.Status.SUCCEEDED
            payment.save()
            _apply_refund_reversal(booking)
            return payment
        if refunded_total <= payment.refunded_amount:
            return payment
        payment.refunded_amount = refunded_total
        if refunded_total >= payment.amount:
            payment.status = Payment.Status.REFUNDED
        payment.save()
        partial = refunded_total < payment.charged_total
        if ambiguous and payment.caution_amount > ZERO and partial:
            booking.needs_attention = True
        if booking.caution_held < ZERO:
            booking.needs_attention = True
        _apply_money_out(booking)
    return payment


def _lock_refund_entry(payment, entry_id, kind):
    """Return the locked refund entry a Stripe refund names, if it is one of payment's."""
    if not entry_id or not str(entry_id).isdigit():
        return None
    return (
        Payment.objects.select_for_update()
        .filter(pk=entry_id, refund_of=payment, kind=kind)
        .first()
    )


def record_stripe_refund(payment_id, refund_id, amount, kind, *, entry_id=None, received_by=None):
    """Record one refund Stripe holds against a payment. Repeating it changes nothing.

    entry_id is the pending entry that reserved the refund, taken from the
    refund's metadata; it is marked succeeded. A caution refund with no entry
    (one this app did not reserve) gets an entry of its own, once per refund
    id. Rental refunds only settle their entry here: the rental money itself
    is counted by record_refund. The refund has already happened at Stripe, so
    it is recorded; if it takes the caution held below zero the booking is
    flagged.

    The one exception is an entry already failed for a reason Stripe gave
    (it reported the refund failed, or refused it). Stripe never moves a
    failed refund back to succeeded, so such a sighting is an out-of-date
    answer: the entry stays failed, the booking is flagged and the failed
    entry is returned for the caller not to count. An entry the app failed
    itself (a timeout with nothing at Stripe yet) is moved to succeeded.
    """
    with transaction.atomic():
        booking, payment = _lock_payment(payment_id)
        entry = Payment.objects.filter(stripe_refund_id=refund_id).first()
        if entry is None:
            entry = _lock_refund_entry(payment, entry_id, kind)
        if entry is None:
            if kind != Payment.Kind.CAUTION_REFUND:
                return None
            entry = Payment(
                booking=booking,
                amount=amount,
                method=payment.method,
                kind=kind,
                refund_of=payment,
                received_by=received_by,
            )
        if entry.status == Payment.Status.SUCCEEDED and entry.stripe_refund_id == refund_id:
            return entry
        if (
            entry.status == Payment.Status.FAILED
            and entry.failure_reason not in Payment.REVIVABLE_FAILURES
        ):
            flag_attention(booking)
            return entry
        entry.status = Payment.Status.SUCCEEDED
        entry.failure_reason = ""
        entry.stripe_refund_id = refund_id
        entry.reference = refund_id
        entry.received_at = entry.received_at or timezone.now()
        entry.save()
        documents.refund_succeeded(entry)
        if booking.caution_held < ZERO:
            flag_attention(booking)
    return entry


def reserve_caution_refund(booking_id, amount, *, received_by=None):
    """Reserve a caution refund through Stripe and return its pending entry.

    The entry is written while the booking is locked and counts against the
    caution held from then on, so two refunds requested at once cannot both be
    approved against the same money. Raises PaymentError when the amount is
    more than the caution still free, or no single card or ACH payment holds
    that much caution.
    """
    amount = positive_amount(amount)
    with transaction.atomic():
        booking = lock_booking(booking_id)
        free = booking.payments.caution_available()
        if amount > free:
            raise PaymentError(f"Amount is more than the caution deposit still held (${free}).")
        for payment in booking.payments.filter(
            kind__in=Payment.RENTAL_KINDS, stripe_payment_intent_id__isnull=False
        ):
            if payment.caution_refundable >= amount:
                return _reserve_refund(
                    booking, payment, amount, Payment.Kind.CAUTION_REFUND, received_by
                )
    raise PaymentError(
        "No card or ACH payment holds that much caution. Refund it by Zelle, Cash App "
        "or bank deposit and record it that way."
    )


def reserve_rental_refund(payment_id, amount, *, received_by=None):
    """Reserve a rental refund through Stripe and return its pending entry.

    Written under the booking lock, and counted in every later limit check
    until Stripe answers.
    """
    amount = positive_amount(amount)
    with transaction.atomic():
        booking, payment = _lock_payment(payment_id)
        if payment.kind not in Payment.RENTAL_KINDS or not payment.stripe_payment_intent_id:
            raise PaymentError("Only card and ACH rental payments can be refunded here.")
        _check_rental_refundable(payment, amount)
        return _reserve_refund(booking, payment, amount, Payment.Kind.RENTAL_REFUND, received_by)


def _check_rental_refundable(payment, amount):
    """Raise PaymentError unless amount of rental money can still be refunded on payment."""
    if payment.status not in (Payment.Status.SUCCEEDED, Payment.Status.REFUNDED):
        raise PaymentError("This payment has not succeeded, so there is nothing to refund.")
    if amount > payment.rental_refundable:
        raise PaymentError(
            f"Amount is more than the rental money left on this payment "
            f"(${payment.rental_refundable})."
        )


def _reserve_refund(booking, payment, amount, kind, received_by):
    return Payment.objects.create(
        booking=booking,
        amount=amount,
        method=payment.method,
        kind=kind,
        status=Payment.Status.PENDING,
        refund_of=payment,
        received_by=received_by,
    )


def complete_refund(entry_id, refund_id):
    """Mark a reserved refund succeeded once Stripe has accepted it.

    A rental refund also raises the refunded total on its payment here, unless
    a webhook got in first and already counted it.
    """
    with transaction.atomic():
        booking_id = Payment.objects.values_list("booking_id", flat=True).get(pk=entry_id)
        lock_booking(booking_id)
        entry = Payment.objects.select_for_update().get(pk=entry_id)
        if entry.status != Payment.Status.PENDING:
            return entry
        record_stripe_refund(
            entry.refund_of_id, refund_id, entry.amount, entry.kind, entry_id=entry.pk
        )
        if entry.kind == Payment.Kind.RENTAL_REFUND:
            parent = Payment.objects.get(pk=entry.refund_of_id)
            record_refund(parent.pk, parent.refunded_amount + entry.amount)
    entry.refresh_from_db()
    return entry


def fail_refund(entry_id, reason):
    """Mark a refund entry failed for reason, releasing the money it reserved.

    An entry the app failed itself that Stripe now reports failed takes
    Stripe's reason, so it can no longer be revived.
    """
    with transaction.atomic():
        booking_id = Payment.objects.values_list("booking_id", flat=True).get(pk=entry_id)
        lock_booking(booking_id)
        entry = Payment.objects.select_for_update().get(pk=entry_id, refund_of__isnull=False)
        if entry.status != Payment.Status.FAILED:
            entry.status = Payment.Status.FAILED
            entry.failure_reason = reason
            entry.save()
        elif entry.failure_reason in Payment.REVIVABLE_FAILURES and reason != entry.failure_reason:
            entry.failure_reason = reason
            entry.save(update_fields=["failure_reason", "updated_at"])
    return entry


def flag_mismatch(payment_id):
    """Note, once, that Stripe charged a different amount than a pending payment asked for.

    The booking is flagged the first time only, so the periodic sync does not
    flag it again every run.
    """
    with transaction.atomic():
        booking, payment = _lock_payment(payment_id)
        if payment.mismatch_flagged_at is None:
            payment.mismatch_flagged_at = timezone.now()
            payment.save(update_fields=["mismatch_flagged_at", "updated_at"])
            flag_attention(booking)
    return payment


def _lock_mismatched(payment_id):
    booking, payment = _lock_payment(payment_id)
    if payment.status != Payment.Status.PENDING or payment.mismatch_flagged_at is None:
        raise PaymentError("This payment is not waiting on a charged-amount mismatch.")
    return booking, payment


def accept_charged_amount(payment_id, charged, *, received_by):
    """Take what Stripe really charged as the payment's amounts, and flag the booking.

    Caution stays as asked (up to what was charged) and the rental part takes
    the difference. The payment stays pending for record_success or
    record_processing to settle; any rental money above what the booking
    owes is then kept and shows as overpaid, refundable to the customer.
    """
    with transaction.atomic():
        booking, payment = _lock_mismatched(payment_id)
        if charged <= ZERO:
            raise PaymentError("Stripe charged nothing on this payment.")
        payment.caution_amount = min(payment.caution_amount, charged)
        payment.amount = charged - payment.caution_amount
        payment.mismatch_flagged_at = None
        payment.received_by = received_by
        payment.save()
        flag_attention(booking)
    return payment


def fail_mismatched_payment(payment_id, *, payment_intent_id, refund_id):
    """Fail a mismatched payment whose whole charge was refunded at Stripe.

    Failed payments never count, so nothing moves on the booking; the refund
    id is kept as the reference and the booking flagged. Later webhooks for
    the refunded charge cannot bring the payment back.
    """
    with transaction.atomic():
        booking, payment = _lock_mismatched(payment_id)
        payment.status = Payment.Status.FAILED
        payment.failure_reason = Payment.FailureReason.AMOUNT_MISMATCH
        payment.stripe_payment_intent_id = payment_intent_id
        payment.reference = refund_id
        payment.save()
        flag_attention(booking)
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
        check_not_recorded(booking, [Payment.Kind.RENTAL_REFUND], method, reference)
        _check_rental_refundable(payment, amount)
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
        documents.refund_succeeded(entry)
    return entry


def reassess_after_change(booking):
    """Re-check a locked booking against its payments after its total changed.

    A booking with payments on it is always flagged, so staff collect or
    refund the difference; a secured booking's status follows the new total.
    """
    if not booking.payments.counted().exists():
        return
    paid, total, deposit = booking.amount_paid, booking.total, booking.deposit_amount
    if booking.status in SECURED_STATUSES:
        if paid >= total:
            booking.status = Booking.Status.PAID
        elif paid >= deposit:
            booking.status = Booking.Status.DEPOSIT_PAID
    booking.needs_attention = True
    booking.save(update_fields=["status", "needs_attention", "updated_at"])


def check_not_recorded(booking, kinds, method, reference):
    """Refuse an entry a locked booking already has: same kind, method and reference.

    Guards against a form submitted twice. Failed entries never moved money,
    so they do not count.
    """
    reference = (reference or "").strip()
    if not reference or not method:
        return
    duplicate = (
        booking.payments.counted()
        .filter(kind__in=kinds, method=method, reference__iexact=reference)
        .exists()
    )
    if duplicate:
        raise PaymentError(
            f"A {Payment.Method(method).label} entry with reference {reference} is already "
            "recorded on this booking."
        )


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


def check_manual_payment(booking, *, method, amount, caution_amount, reference):
    """Validate a manual payment against a locked booking; return the cleaned amounts.

    Checks everything except payments still in progress, so it can run before
    open checkouts are closed: a payment refused here leaves them untouched.
    """
    if method not in Payment.MANUAL_METHODS:
        raise PaymentError("Manual payments must be Zelle, Cash App or bank deposit.")
    amount = _clean_amount(amount, "Amount")
    caution_amount = _clean_amount(caution_amount, "Caution amount")
    if amount + caution_amount <= ZERO:
        raise PaymentError("Enter an amount greater than zero.")
    if booking.status == Booking.Status.CANCELLED:
        raise PaymentError("This booking is cancelled. Reopen it before recording a payment.")
    check_not_recorded(booking, Payment.RENTAL_KINDS, method, reference)
    if amount > ZERO:
        check_promo_available(booking)
    balance = booking.balance_due
    if amount > balance:
        raise PaymentError(f"Amount is more than the balance due (${balance}).")
    caution_due = booking.caution_due
    if caution_amount > caution_due:
        raise PaymentError(
            f"Caution amount is more than the caution still to collect (${caution_due})."
        )
    return amount, caution_amount


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
    with transaction.atomic():
        booking = lock_booking(booking.pk)
        amount, caution_amount = check_manual_payment(
            booking,
            method=method,
            amount=amount,
            caution_amount=caution_amount,
            reference=reference,
        )
        if booking.payments.open_checkouts().exists():
            raise PaymentError(
                "A card or bank payment for this booking is still in progress. "
                "It must fail or be expired before a manual payment is recorded."
            )
        paid, balance = booking.amount_paid, booking.balance_due
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
            _money_in(booking, payment)
        else:
            documents.payment_succeeded(payment, confirmed=False)
    return payment


def _release_caution(booking, kind, *, amount, method, reference, received_at, received_by):
    amount = positive_amount(amount)
    with transaction.atomic():
        booking = lock_booking(booking.pk)
        check_not_recorded(booking, [kind], method, reference)
        free = booking.payments.caution_available()
        if amount > free:
            raise PaymentError(f"Amount is more than the caution deposit still held (${free}).")
        entry = Payment.objects.create(
            booking=booking,
            amount=amount,
            method=method,
            kind=kind,
            status=Payment.Status.SUCCEEDED,
            reference=reference,
            received_by=received_by,
            received_at=received_at,
        )
        if kind == Payment.Kind.CAUTION_REFUND:
            documents.refund_succeeded(entry)
        return entry


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
