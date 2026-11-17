"""Stripe integration: Checkout sessions and webhook event handling.

Amounts are always computed here from the booking. Nothing the browser sends
can set or change what is charged or what is credited.
"""

import json
import logging
from datetime import timedelta
from decimal import Decimal

import stripe
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.bookings.models import Booking
from apps.payments.models import Payment, StripeEvent, UnmatchedStripeEvent
from apps.payments.services import ledger
from apps.payments.services.ledger import PaymentError

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")
CURRENCY = "usd"
# Metadata tag on refunds of rental money issued from the admin.
RENTAL_REFUND = "rental_refund"
# Stripe's shortest allowed session lifetime is 30 minutes.
CHECKOUT_SESSION_MINUTES = 31
HOLD_GRACE_MINUTES = 5
# Stripe is called while a booking row is locked, so calls are kept short and
# are not retried: a timeout fails the request and leaves nothing behind.
STRIPE_TIMEOUT_SECONDS = 10
IN_PROGRESS = "A payment for this booking is already being processed."
MISMATCHED = (
    "Stripe charged a different amount than this booking's checkout asked for. "
    "Use the Resolve mismatched payment action first."
)
# Times an unmatched event is retried (every 15 minutes: one day) before it is
# left for staff in the admin.
UNMATCHED_RETRY_LIMIT = 96
# Pending payments and refunds older than this are checked by the periodic sync.
SYNC_AFTER_MINUTES = 15
# Bookings paid through Stripe are also synced hourly while they changed this
# recently, or their event is this close, to catch refunds made at Stripe.
RECENT_CHANGE_DAYS = 7
UPCOMING_EVENT_DAYS = 30
# A refund entry still pending this long with no refund at Stripe never got
# there (the request timed out before Stripe saw it), so it is failed.
PENDING_REFUND_TIMEOUT_MINUTES = 15
FAILED_REFUND_STATUSES = ("failed", "canceled")
# A refund this app sends back for a checkout charged the wrong amount.
MISMATCH_REFUND = "mismatch_refund"
Reason = Payment.FailureReason

stripe.default_http_client = stripe.new_default_http_client(timeout=STRIPE_TIMEOUT_SECONDS)
stripe.max_network_retries = 0


class InvalidWebhookError(Exception):
    """Raised when a webhook request cannot be trusted or parsed."""


def to_cents(amount):
    """Convert a dollar Decimal to whole cents."""
    return int(amount * 100)


def from_cents(cents):
    """Convert whole cents to a dollar Decimal."""
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


def _secret_key():
    if not settings.STRIPE_SECRET_KEY:
        raise ImproperlyConfigured("STRIPE_SECRET_KEY is not set.")
    return settings.STRIPE_SECRET_KEY


def _settle_open_checkouts(booking, reuse=None):
    """Deal with a locked booking's unfinished Checkout sessions before more money is taken.

    Each is looked up at Stripe. An open session is expired there and its
    payment marked failed, unless it asks for exactly what reuse describes
    (kind, amount, caution), in which case that session is returned to be used
    again: a double-click gets the same checkout, not a second one. Raises
    PaymentError when a session is already paid or clearing. The caller holds
    the booking lock throughout, so no other request can open a checkout or
    record a payment in between.
    """
    for payment in booking.payments.open_checkouts().order_by("pk"):
        if payment.mismatch_flagged_at is not None:
            raise PaymentError(MISMATCHED)
        session_id = payment.stripe_checkout_session_id
        if session_id:
            session = stripe.checkout.Session.retrieve(session_id, api_key=_secret_key())
            if session["status"] == "open":
                if reuse == (payment.kind, payment.amount, payment.caution_amount):
                    return session
                try:
                    stripe.checkout.Session.expire(session_id, api_key=_secret_key())
                except stripe.InvalidRequestError:
                    raise PaymentError(IN_PROGRESS) from None
            elif session["status"] != "expired":
                raise PaymentError(IN_PROGRESS)
        ledger.record_failure(payment.pk)
    return None


def close_open_checkouts(booking):
    """Expire the booking's unfinished Checkout sessions so staff can take money by hand.

    Raises PaymentError when one of them is already paid or clearing.
    """
    with transaction.atomic():
        _settle_open_checkouts(ledger.lock_booking(booking.pk))


def record_manual_payment(booking, **details):
    """Record a manual payment, closing the booking's unfinished checkouts first.

    One transaction holds the booking lock throughout. The payment itself
    (amount, caution, duplicate reference) is checked before anything is
    expired at Stripe, so a refused payment leaves open checkouts as they were.
    details are ledger.record_manual_payment's keyword arguments.
    """
    with transaction.atomic():
        locked = ledger.lock_booking(booking.pk)
        ledger.check_manual_payment(
            locked,
            method=details["method"],
            amount=details["amount"],
            caution_amount=details.get("caution_amount", ZERO),
            reference=details["reference"],
        )
        _settle_open_checkouts(locked)
        return ledger.record_manual_payment(locked, **details)


class Resolution:
    """What staff do about a checkout Stripe charged the wrong amount for."""

    ACCEPT = "accept"
    REFUND = "refund"
    CHOICES = [
        (ACCEPT, "Record the amount Stripe actually charged (the booking is flagged)"),
        (REFUND, "Fail the payment and refund all of it at Stripe"),
    ]


def resolve_mismatch(payment, resolution, *, by):
    """Settle a pending payment Stripe charged a different amount for.

    The session is read again from Stripe under the booking lock; nothing the
    caller sends sets an amount. ACCEPT records what Stripe charged, as
    paid or as still clearing. REFUND fails the payment and refunds the whole
    charge at Stripe, in the same transaction: if Stripe cannot be reached
    nothing changes, and a retry reuses the same refund (idempotency key).
    A charge still clearing cannot be refunded yet. Returns the payment.
    """
    with transaction.atomic():
        ledger.lock_booking(payment.booking_id)
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.status != Payment.Status.PENDING or payment.mismatch_flagged_at is None:
            raise PaymentError("This payment is not waiting on a charged-amount mismatch.")
        session = stripe.checkout.Session.retrieve(
            payment.stripe_checkout_session_id, api_key=_secret_key()
        )
        intent_id = session.get("payment_intent")
        cents = session.get("amount_total") or 0
        if resolution == Resolution.ACCEPT:
            if session.get("currency") != CURRENCY:
                raise PaymentError(
                    f"Stripe charged in {str(session.get('currency')).upper()}, not USD. "
                    "Refund it instead."
                )
            ledger.accept_charged_amount(payment.pk, from_cents(cents), received_by=by)
            if session.get("payment_status") == "paid":
                ledger.record_success(payment.pk, payment_intent_id=intent_id)
            else:
                ledger.record_processing(payment.pk, payment_intent_id=intent_id)
        elif resolution == Resolution.REFUND:
            if session.get("payment_status") != "paid" or not intent_id:
                raise PaymentError(
                    "This payment is still clearing, so it cannot be refunded yet. "
                    "Try again once it has cleared, or record the amount charged."
                )
            refund = stripe.Refund.create(
                api_key=_secret_key(),
                idempotency_key=f"rentbook-mismatch-refund-{payment.pk}",
                payment_intent=intent_id,
                amount=cents,
                metadata={
                    "kind": MISMATCH_REFUND,
                    "booking_id": str(payment.booking_id),
                    "payment_id": str(payment.pk),
                },
            )
            ledger.fail_mismatched_payment(
                payment.pk, payment_intent_id=intent_id, refund_id=refund["id"]
            )
        else:
            raise PaymentError("Choose how to resolve the payment.")
    payment.refresh_from_db()
    return payment


def _amount_due(booking, kind):
    """Return (rental amount, caution amount) a checkout of kind would charge now."""
    if not (ledger.has_live_hold(booking) or booking.status == Booking.Status.DEPOSIT_PAID):
        raise PaymentError("This booking is not open for payment.")
    ledger.check_promo_available(booking)

    paid, total = booking.amount_paid, booking.total
    if kind == Payment.Kind.DEPOSIT:
        amount = booking.deposit_amount - paid
    elif kind == Payment.Kind.FULL:
        if paid > ZERO:
            raise PaymentError("This booking is part paid. Pay the balance instead.")
        amount = total
    elif kind == Payment.Kind.BALANCE:
        if paid == ZERO:
            raise PaymentError("Nothing has been paid yet. Pay the deposit or in full.")
        amount = total - paid
    else:
        raise PaymentError("Checkout is only for deposit, full or balance payments.")
    if amount <= ZERO:
        raise PaymentError("Nothing is due for this payment.")

    caution = ZERO
    if kind in (Payment.Kind.FULL, Payment.Kind.BALANCE):
        caution = max(booking.caution_fee - booking.payments.caution_collected(), ZERO)
    return amount, caution


def _line_item(name, amount):
    return {
        "quantity": 1,
        "price_data": {
            "currency": CURRENCY,
            "unit_amount": to_cents(amount),
            "product_data": {"name": name},
        },
    }


def create_checkout_session(booking, kind, *, success_url, cancel_url):
    """Create a Stripe Checkout session for a deposit, full or balance payment.

    The amount comes from the booking, never from the caller. Full and balance
    payments also collect any caution fee not yet taken, as a separate line.
    The stock hold is extended to outlast the session.

    Everything happens in one transaction holding the booking lock: earlier
    sessions are expired at Stripe, the session is created, and the pending
    payment is saved with its session id before the lock is released. Two
    requests at once therefore run one after the other, and the second is
    handed the first one's session. If Stripe fails or times out, nothing is
    saved.
    """
    api_key = _secret_key()
    with transaction.atomic():
        booking = ledger.lock_booking(booking.pk)
        amount, caution = _amount_due(booking, kind)
        open_session = _settle_open_checkouts(booking, reuse=(kind, amount, caution))
        if open_session is not None:
            return open_session

        expires_at = timezone.now() + timedelta(minutes=CHECKOUT_SESSION_MINUTES)
        if ledger.has_live_hold(booking):
            keep_until = expires_at + timedelta(minutes=HOLD_GRACE_MINUTES)
            booking.hold_expires_at = max(booking.hold_expires_at, keep_until)
            booking.save(update_fields=["hold_expires_at", "updated_at"])
        payment = Payment.objects.create(
            booking=booking,
            amount=amount,
            caution_amount=caution,
            method=Payment.Method.CARD,
            kind=kind,
        )

        label = f"Booking #{booking.pk} for {booking.event_date:%b %d, %Y}"
        line_items = [_line_item(f"{payment.get_kind_display()}: {label}", amount)]
        if caution > ZERO:
            line_items.append(_line_item(f"Refundable caution deposit: {label}", caution))
        metadata = {
            "booking_id": str(booking.pk),
            "kind": str(kind),
            "payment_id": str(payment.pk),
        }
        session = stripe.checkout.Session.create(
            api_key=api_key,
            idempotency_key=f"rentbook-payment-{payment.pk}",
            mode="payment",
            payment_method_types=["card", "us_bank_account"],
            line_items=line_items,
            metadata=metadata,
            payment_intent_data={"metadata": metadata},
            client_reference_id=str(booking.pk),
            customer_email=booking.email,
            success_url=success_url,
            cancel_url=cancel_url,
            expires_at=int(expires_at.timestamp()),
        )
        payment.stripe_checkout_session_id = session["id"]
        payment.save(update_fields=["stripe_checkout_session_id", "updated_at"])
    return session


def parse_event(payload, signature_header):
    """Verify a webhook's signature and return the event as a dict.

    Raises InvalidWebhookError when the signature is missing or wrong, or the
    webhook secret is not configured.
    """
    secret = settings.STRIPE_WEBHOOK_SECRET
    if not secret or not signature_header:
        raise InvalidWebhookError("Missing webhook secret or signature.")
    try:
        text = payload.decode("utf-8")
        stripe.WebhookSignature.verify_header(
            text, signature_header, secret, stripe.Webhook.DEFAULT_TOLERANCE
        )
        return json.loads(text)
    except (ValueError, stripe.SignatureVerificationError) as error:
        raise InvalidWebhookError(str(error)) from error


def _payment_for_session(session):
    """Find our payment for a Checkout session by its session id.

    The id is saved in the same transaction that created the session, so an
    event that matches nothing is kept and retried rather than matched by its
    metadata (which would lock the payment before its booking).
    """
    payment = Payment.objects.filter(stripe_checkout_session_id=session["id"]).first()
    if payment is None:
        logger.warning("Stripe session %s matches no payment.", session["id"])
    return payment


def _amount_matches(payment, session):
    """Check Stripe charged exactly what we asked for (rental plus caution).

    A mismatch leaves the payment pending for staff to resolve and flags the
    booking, once: a payment already noted as mismatched is not flagged again.
    A payment staff already failed and refunded for it is never credited.
    """
    if payment.failure_reason == Reason.AMOUNT_MISMATCH:
        return False
    expected = to_cents(payment.charged_total)
    if session.get("amount_total") == expected and session.get("currency") == CURRENCY:
        return True
    if payment.mismatch_flagged_at is None:
        logger.error(
            "Stripe session %s total %s %s does not match payment %s (%s cents). Not credited.",
            session["id"],
            session.get("amount_total"),
            session.get("currency"),
            payment.pk,
            expected,
        )
    ledger.flag_mismatch(payment.pk)
    return False


def _on_checkout_completed(session):
    payment = _payment_for_session(session)
    if payment is None:
        return False
    if not _amount_matches(payment, session):
        return True
    intent_id = session.get("payment_intent")
    if session.get("payment_status") == "paid":
        ledger.record_success(payment.pk, payment_intent_id=intent_id)
    elif session.get("payment_status") == "unpaid":
        ledger.record_processing(payment.pk, payment_intent_id=intent_id)


def _on_async_payment_succeeded(session):
    payment = _payment_for_session(session)
    if payment is None:
        return False
    if not _amount_matches(payment, session):
        return True
    ledger.record_success(
        payment.pk, payment_intent_id=session.get("payment_intent"), method=Payment.Method.ACH
    )


def _on_async_payment_failed(session):
    payment = _payment_for_session(session)
    if payment is None:
        return False
    ledger.record_failure(payment.pk, method=Payment.Method.ACH)
    return True


def _on_checkout_expired(session):
    """An abandoned checkout can no longer be paid, so it stops blocking the booking."""
    payment = _payment_for_session(session)
    if payment is None:
        return False
    ledger.record_failure(payment.pk)
    return True


def _refund_entry_id(refund):
    return (refund.get("metadata") or {}).get("entry_id")


def _refund_entry(payment, refund):
    """The ledger entry for a Stripe refund, by refund id or the entry id in its metadata."""
    entry = Payment.objects.filter(stripe_refund_id=refund["id"]).first()
    entry_id = _refund_entry_id(refund)
    if entry is None and entry_id and str(entry_id).isdigit():
        entry = Payment.objects.filter(pk=entry_id, refund_of=payment).first()
    return entry


def _fail_refund_entry(payment, refund):
    """Mark failed the entry for a refund Stripe says did not go through.

    Returns (entry, was_counted): was_counted is True when the entry had
    succeeded, so its money had been counted as sent back.
    """
    entry = _refund_entry(payment, refund)
    if entry is None:
        return None, False
    was_counted = entry.status == Payment.Status.SUCCEEDED
    ledger.fail_refund(entry.pk, Reason.STRIPE_FAILED)
    return entry, was_counted


def _list_refunds(payment):
    """Every refund Stripe holds for a payment, reading all pages."""
    listing = stripe.Refund.list(
        payment_intent=payment.stripe_payment_intent_id, limit=100, api_key=_secret_key()
    )
    return list(listing.auto_paging_iter())


def _fail_stale_pending_refunds(payment, listed_refunds):
    """Fail refund entries Stripe never received, once they are old enough to be sure.

    A refund request that timed out may still have reached Stripe; it is only
    failed when no refund at Stripe names it and it has been pending longer
    than PENDING_REFUND_TIMEOUT_MINUTES. Returns how many were failed.
    """
    listed_entry_ids = {str(_refund_entry_id(refund)) for refund in listed_refunds}
    listed_refund_ids = {refund["id"] for refund in listed_refunds}
    cutoff = timezone.now() - timedelta(minutes=PENDING_REFUND_TIMEOUT_MINUTES)
    stale = payment.refunds.filter(status=Payment.Status.PENDING, created_at__lt=cutoff)
    failed = 0
    for entry in stale:
        if str(entry.pk) in listed_entry_ids or entry.stripe_refund_id in listed_refund_ids:
            continue
        ledger.fail_refund(entry.pk, Reason.NOT_RECEIVED)
        failed += 1
    return failed


def _current_refunds(listed):
    """Each listed refund fetched again from Stripe, as it stands now."""
    return [stripe.Refund.retrieve(refund["id"], api_key=_secret_key()) for refund in listed]


def sync_refunds(payment, *, reported_total=ZERO, received_by=None, failed_ids=()):
    """Bring the ledger in line with every refund Stripe holds for a payment.

    Refunds tagged as caution refunds become caution refund entries, once each.
    Everything else counts as rental money refunded; refunds without the app's
    tag (made in the Stripe dashboard) are treated as ambiguous. A refund this
    app reserved settles its pending entry: succeeded, or failed if Stripe
    says it did not go through. Safe to repeat. reported_total is the refunded
    total a webhook stated, in case the list is behind it. failed_ids are
    refunds a refund.failed event reported, in case the list is behind that.

    The list only says which refunds exist. Each one is fetched again while
    the booking is locked and only that answer is acted on, so a sync that
    read its list before another sync or a refund.failed webhook changed the
    ledger cannot undo that change.

    A rental refund already counted that has since failed is taken back off
    the refunded total, and only that: refunds missing from a lagging list
    are never taken back. A refund whose entry Stripe already reported failed
    is never counted again, whatever an answer says.
    """
    listed = _list_refunds(payment)
    failed_ids = set(failed_ids)
    with transaction.atomic():
        booking = ledger.lock_booking(payment.booking_id)
        payment = Payment.objects.get(pk=payment.pk)
        if payment.failure_reason == Reason.AMOUNT_MISMATCH:
            return
        refunds = _current_refunds(listed)
        rental = caution = taken_back = not_counted = ZERO
        ambiguous = reversed_entry = False
        for refund in refunds:
            amount = from_cents(refund["amount"])
            kind = (refund.get("metadata") or {}).get("kind")
            if refund["status"] in FAILED_REFUND_STATUSES or refund["id"] in failed_ids:
                entry, was_counted = _fail_refund_entry(payment, refund)
                reversed_entry |= was_counted
                counted = was_counted if entry is not None else refund["id"] in failed_ids
                if counted and kind != Payment.Kind.CAUTION_REFUND:
                    taken_back += amount
                continue
            is_caution = kind == Payment.Kind.CAUTION_REFUND
            entry = ledger.record_stripe_refund(
                payment.pk,
                refund["id"],
                amount,
                Payment.Kind.CAUTION_REFUND if is_caution else Payment.Kind.RENTAL_REFUND,
                entry_id=_refund_entry_id(refund),
                received_by=received_by,
            )
            if entry is not None and entry.status == Payment.Status.FAILED:
                not_counted += amount
            elif is_caution:
                caution += amount
            else:
                rental += amount
                ambiguous |= kind != RENTAL_REFUND
        unlisted = reported_total - rental - caution - not_counted
        if unlisted > ZERO:
            rental += unlisted
            ambiguous = True
        if _fail_stale_pending_refunds(payment, refunds) or reversed_entry:
            ledger.flag_attention(booking)
        if taken_back > ZERO:
            counted_before = Payment.objects.get(pk=payment.pk).refunded_amount
            rental = max(rental, counted_before - taken_back)
        ledger.record_refund(
            payment.pk, rental, ambiguous=ambiguous, allow_decrease=taken_back > ZERO
        )


# Errors Stripe raises for a 4xx answer, used when an error carries no HTTP status.
REFUSED_ERRORS = (
    stripe.InvalidRequestError,
    stripe.AuthenticationError,
    stripe.PermissionError,
    stripe.CardError,
    stripe.RateLimitError,
    stripe.IdempotencyError,
)


def _refused_by_stripe(error):
    """True when Stripe answered a request with a 4xx: it was refused and nothing was made."""
    if isinstance(error, stripe.APIConnectionError):
        return False
    status = getattr(error, "http_status", None)
    if status is not None:
        return 400 <= status < 500
    return isinstance(error, REFUSED_ERRORS)


def _issue_refund(entry, kind):
    """Send a reserved refund through Stripe and settle its entry with the answer.

    The idempotency key is the entry itself, so a retry of one request cannot
    refund twice and two different refunds are never collapsed into one. When
    Stripe refuses the request (a 4xx answer), the entry is failed and its
    money released. Any other error (Stripe unreachable, a 5xx, an unknown
    answer) leaves the outcome unknown: the entry stays pending, still
    reserving its amount, the booking is flagged, and the sync settles it from
    what Stripe holds.
    """
    payment = entry.refund_of
    try:
        refund = stripe.Refund.create(
            api_key=_secret_key(),
            idempotency_key=f"rentbook-refund-entry-{entry.pk}",
            payment_intent=payment.stripe_payment_intent_id,
            amount=to_cents(entry.amount),
            metadata={
                "kind": str(kind),
                "booking_id": str(payment.booking_id),
                "payment_id": str(payment.pk),
                "entry_id": str(entry.pk),
            },
        )
    except stripe.StripeError as error:
        if _refused_by_stripe(error):
            ledger.fail_refund(entry.pk, Reason.STRIPE_REFUSED)
        else:
            with transaction.atomic():
                ledger.flag_attention(ledger.lock_booking(payment.booking_id))
        raise
    if refund["status"] in FAILED_REFUND_STATUSES:
        ledger.fail_refund(entry.pk, Reason.STRIPE_FAILED)
        raise PaymentError("Stripe could not send this refund. Nothing was refunded.")
    ledger.complete_refund(entry.pk, refund["id"])
    try:
        sync_refunds(payment, received_by=entry.received_by)
    except stripe.StripeError:
        logger.exception(
            "Refund on payment %s is recorded but could not be re-checked against Stripe.",
            payment.pk,
        )


def refund_caution(booking, amount, *, received_by=None):
    """Send caution deposit money back to the card or bank account it came from.

    The amount is reserved on the ledger first, then sent. The refund is tagged
    so it is recorded as a caution refund, never as a rental refund. Returns
    the payment it was refunded against.
    """
    entry = ledger.reserve_caution_refund(booking.pk, amount, received_by=received_by)
    _issue_refund(entry, Payment.Kind.CAUTION_REFUND)
    return entry.refund_of


def refund_rental(payment, amount, *, received_by=None):
    """Refund rental money on a card or ACH payment through Stripe.

    The amount is reserved on the ledger first, then sent. The booking is then
    re-evaluated exactly as for any refund.
    """
    entry = ledger.reserve_rental_refund(payment.pk, amount, received_by=received_by)
    _issue_refund(entry, RENTAL_REFUND)
    return entry.refund_of


def _sync_checkout(payment):
    """Settle one pending checkout payment from its session (and intent) at Stripe."""
    session = stripe.checkout.Session.retrieve(
        payment.stripe_checkout_session_id, api_key=_secret_key()
    )
    if session["status"] == "expired":
        ledger.record_failure(payment.pk)
        return
    if session["status"] != "complete":
        return
    if not _amount_matches(payment, session):
        return
    intent_id = session.get("payment_intent")
    if session.get("payment_status") == "paid":
        ledger.record_success(payment.pk, payment_intent_id=intent_id)
        return
    if session.get("payment_status") != "unpaid" or not intent_id:
        return
    intent = stripe.PaymentIntent.retrieve(intent_id, api_key=_secret_key())
    if intent["status"] == "succeeded":
        ledger.record_success(payment.pk, payment_intent_id=intent_id, method=Payment.Method.ACH)
    elif intent["status"] in ("requires_payment_method", "canceled"):
        ledger.record_failure(payment.pk, method=Payment.Method.ACH)
    elif payment.method != Payment.Method.ACH:
        ledger.record_processing(payment.pk, payment_intent_id=intent_id)


def sync_booking(booking):
    """Bring a booking's payments and refunds in line with what Stripe holds.

    Pending checkouts are settled from their sessions: paid, still clearing,
    failed or expired. Every card or ACH payment then has its refunds synced,
    which also settles refund entries left pending (for example after a
    timeout). Safe to repeat. Raises stripe.StripeError if Stripe cannot be
    reached; whatever was settled before that is kept.
    """
    pending = booking.payments.open_checkouts().filter(stripe_checkout_session_id__isnull=False)
    for payment in pending.order_by("pk"):
        _sync_checkout(payment)
    paid = booking.payments.filter(
        kind__in=Payment.RENTAL_KINDS,
        status__in=(Payment.Status.SUCCEEDED, Payment.Status.REFUNDED),
        stripe_payment_intent_id__isnull=False,
    )
    for payment in paid.order_by("pk"):
        sync_refunds(payment)


def sync_stale_bookings():
    """Sync every booking with a payment or refund pending for SYNC_AFTER_MINUTES.

    Returns how many bookings were synced. A booking Stripe cannot be reached
    for is logged and left for the next run.
    """
    cutoff = timezone.now() - timedelta(minutes=SYNC_AFTER_MINUTES)
    booking_ids = (
        Payment.objects.filter(status=Payment.Status.PENDING, created_at__lt=cutoff)
        .values_list("booking_id", flat=True)
        .distinct()
    )
    return _sync_each(Booking.objects.filter(pk__in=list(booking_ids)))


def sync_recent_paid_bookings():
    """Sync bookings paid through Stripe that changed lately or have an event coming up.

    Catches refunds and disputes made in the Stripe dashboard whose webhook
    was missed: every booking with a card or ACH payment that it or its
    payments changed in the last RECENT_CHANGE_DAYS, or whose event is in the
    next UPCOMING_EVENT_DAYS. Returns how many were synced.
    """
    now = timezone.now()
    today = timezone.localdate()
    changed_since = now - timedelta(days=RECENT_CHANGE_DAYS)
    paid = Payment.objects.filter(
        kind__in=Payment.RENTAL_KINDS,
        status__in=(Payment.Status.SUCCEEDED, Payment.Status.REFUNDED),
        stripe_payment_intent_id__isnull=False,
    )
    recent = paid.filter(
        Q(updated_at__gte=changed_since)
        | Q(booking__updated_at__gte=changed_since)
        | Q(
            booking__event_date__gte=today,
            booking__event_date__lte=today + timedelta(days=UPCOMING_EVENT_DAYS),
        )
    )
    booking_ids = recent.values_list("booking_id", flat=True).distinct()
    return _sync_each(Booking.objects.filter(pk__in=list(booking_ids)))


def _sync_each(bookings):
    """Sync each booking; one Stripe cannot reach is logged and left for the next run."""
    synced = 0
    for booking in bookings.order_by("pk"):
        try:
            sync_booking(booking)
        except stripe.StripeError:
            logger.exception("Booking %s could not be synced with Stripe.", booking.pk)
        else:
            synced += 1
    return synced


def _payment_for_intent(stripe_object):
    intent_id = stripe_object.get("payment_intent")
    payment = (
        Payment.objects.filter(stripe_payment_intent_id=intent_id).first() if intent_id else None
    )
    if payment is None:
        logger.warning("Stripe %s matches no payment.", stripe_object["id"])
    return payment


def _on_charge_refunded(charge):
    payment = _payment_for_intent(charge)
    if payment is None:
        return False
    sync_refunds(payment, reported_total=from_cents(charge["amount_refunded"]))
    return True


def _on_refund_failed(refund):
    """A refund we counted did not reach the customer: take it back and flag the booking."""
    payment = _payment_for_intent(refund)
    if payment is None:
        return False
    ledger.flag_attention(ledger.lock_booking(payment.booking_id))
    sync_refunds(payment, failed_ids=[refund["id"]])
    return True


HANDLERS = {
    "checkout.session.completed": _on_checkout_completed,
    "checkout.session.async_payment_succeeded": _on_async_payment_succeeded,
    "checkout.session.async_payment_failed": _on_async_payment_failed,
    "checkout.session.expired": _on_checkout_expired,
    "charge.refunded": _on_charge_refunded,
    "refund.failed": _on_refund_failed,
}


def handle_event(event):
    """Process a verified Stripe event once. Returns False for a repeated event.

    The event id is recorded in the same transaction as the changes it causes,
    so a failure part-way leaves nothing behind and Stripe's retry starts clean.
    An event that matches no payment is kept in UnmatchedStripeEvent for the
    periodic sync to match again.
    """
    with transaction.atomic():
        try:
            with transaction.atomic():
                StripeEvent.objects.create(event_id=event["id"], type=event["type"])
        except IntegrityError:
            return False
        handler = HANDLERS.get(event["type"])
        if handler is not None and handler(event["data"]["object"]) is False:
            UnmatchedStripeEvent.objects.get_or_create(
                event_id=event["id"],
                defaults={
                    "type": event["type"],
                    "object_id": event["data"]["object"].get("id", ""),
                    "payload": event,
                },
            )
    return True


def retry_unmatched():
    """Run each kept unmatched event through its handler again; return how many matched.

    An event still matching nothing counts an attempt, and is no longer
    retried after UNMATCHED_RETRY_LIMIT attempts. Each event is handled in its
    own transaction, so one failure does not undo the others: any error is
    logged (and so reported to Sentry), counts an attempt, and the loop moves
    on to the next event.
    """
    matched = 0
    open_ids = UnmatchedStripeEvent.objects.filter(
        resolved_at__isnull=True, attempts__lt=UNMATCHED_RETRY_LIMIT
    ).values_list("pk", flat=True)
    for pk in list(open_ids):
        try:
            with transaction.atomic():
                row = (
                    UnmatchedStripeEvent.objects.select_for_update(skip_locked=True)
                    .filter(pk=pk, resolved_at__isnull=True)
                    .first()
                )
                if row is None:
                    continue
                handler = HANDLERS[row.type]
                if handler(row.payload["data"]["object"]) is False:
                    row.attempts += 1
                else:
                    row.resolved_at = timezone.now()
                    matched += 1
                row.save(update_fields=["attempts", "resolved_at", "updated_at"])
        except Exception:
            logger.exception("Unmatched Stripe event %s could not be retried.", pk)
            UnmatchedStripeEvent.objects.filter(pk=pk).update(attempts=F("attempts") + 1)
    return matched
