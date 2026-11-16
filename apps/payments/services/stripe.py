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
from django.utils import timezone

from apps.bookings.models import Booking
from apps.payments.models import Payment, StripeEvent
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
    """Find our payment for a Checkout session, by session id or signed metadata."""
    payment = Payment.objects.filter(stripe_checkout_session_id=session["id"]).first()
    if payment is None:
        payment_id = (session.get("metadata") or {}).get("payment_id", "")
        if payment_id.isdigit():
            claimed = Payment.objects.filter(
                pk=payment_id, stripe_checkout_session_id__isnull=True
            ).update(stripe_checkout_session_id=session["id"])
            if claimed:
                payment = Payment.objects.get(pk=payment_id)
    if payment is None:
        logger.warning("Stripe session %s matches no payment.", session["id"])
    return payment


def _amount_matches(payment, session):
    """Check Stripe charged exactly what we asked for (rental plus caution)."""
    expected = to_cents(payment.charged_total)
    if session.get("amount_total") == expected and session.get("currency") == CURRENCY:
        return True
    logger.error(
        "Stripe session %s total %s %s does not match payment %s (%s cents). Not credited.",
        session["id"],
        session.get("amount_total"),
        session.get("currency"),
        payment.pk,
        expected,
    )
    ledger.flag_attention(ledger.lock_booking(payment.booking_id))
    return False


def _on_checkout_completed(session):
    payment = _payment_for_session(session)
    if payment is None or not _amount_matches(payment, session):
        return
    intent_id = session.get("payment_intent")
    if session.get("payment_status") == "paid":
        ledger.record_success(payment.pk, payment_intent_id=intent_id)
    elif session.get("payment_status") == "unpaid":
        ledger.record_processing(payment.pk, payment_intent_id=intent_id)


def _on_async_payment_succeeded(session):
    payment = _payment_for_session(session)
    if payment is None or not _amount_matches(payment, session):
        return
    ledger.record_success(
        payment.pk, payment_intent_id=session.get("payment_intent"), method=Payment.Method.ACH
    )


def _on_async_payment_failed(session):
    payment = _payment_for_session(session)
    if payment is not None:
        ledger.record_failure(payment.pk, method=Payment.Method.ACH)


def _on_checkout_expired(session):
    """An abandoned checkout can no longer be paid, so it stops blocking the booking."""
    payment = Payment.objects.filter(stripe_checkout_session_id=session["id"]).first()
    if payment is not None:
        ledger.record_failure(payment.pk)


def _refund_entry_id(refund):
    return (refund.get("metadata") or {}).get("entry_id")


def _fail_refund_entry(payment, refund):
    """Mark failed the entry for a refund Stripe says did not go through."""
    entry = Payment.objects.filter(stripe_refund_id=refund["id"]).first()
    entry_id = _refund_entry_id(refund)
    if entry is None and entry_id and str(entry_id).isdigit():
        entry = Payment.objects.filter(pk=entry_id, refund_of=payment).first()
    if entry is not None:
        ledger.fail_refund(entry.pk)


def sync_refunds(payment, *, reported_total=ZERO, received_by=None):
    """Bring the ledger in line with every refund Stripe holds for a payment.

    Refunds tagged as caution refunds become caution refund entries, once each.
    Everything else counts as rental money refunded; refunds without the app's
    tag (made in the Stripe dashboard) are treated as ambiguous. A refund this
    app reserved settles its pending entry: succeeded, or failed if Stripe
    says it did not go through. Safe to repeat. reported_total is the refunded
    total a webhook stated, in case the list is behind it.
    """
    refunds = stripe.Refund.list(
        payment_intent=payment.stripe_payment_intent_id, limit=100, api_key=_secret_key()
    )
    with transaction.atomic():
        ledger.lock_booking(payment.booking_id)
        rental = caution = ZERO
        ambiguous = False
        for refund in refunds["data"]:
            if refund["status"] in ("failed", "canceled"):
                _fail_refund_entry(payment, refund)
                continue
            amount = from_cents(refund["amount"])
            kind = (refund.get("metadata") or {}).get("kind")
            if kind == Payment.Kind.CAUTION_REFUND:
                caution += amount
            else:
                rental += amount
                ambiguous |= kind != RENTAL_REFUND
                kind = Payment.Kind.RENTAL_REFUND
            ledger.record_stripe_refund(
                payment.pk,
                refund["id"],
                amount,
                kind,
                entry_id=_refund_entry_id(refund),
                received_by=received_by,
            )
        unlisted = reported_total - rental - caution
        if unlisted > ZERO:
            rental += unlisted
            ambiguous = True
        ledger.record_refund(payment.pk, rental, ambiguous=ambiguous)


def _issue_refund(entry, kind):
    """Send a reserved refund through Stripe and settle its entry with the answer.

    The idempotency key is the entry itself, so a retry of one request cannot
    refund twice and two different refunds are never collapsed into one. When
    Stripe refuses, the entry is failed and its money released. When Stripe
    cannot be reached the outcome is unknown: the entry stays pending, still
    reserving its amount, and the booking is flagged.
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
    except stripe.APIConnectionError:
        with transaction.atomic():
            ledger.flag_attention(ledger.lock_booking(payment.booking_id))
        raise
    except stripe.StripeError:
        ledger.fail_refund(entry.pk)
        raise
    if refund["status"] in ("failed", "canceled"):
        ledger.fail_refund(entry.pk)
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
    if payment is not None:
        sync_refunds(payment, reported_total=from_cents(charge["amount_refunded"]))


def _on_refund_failed(refund):
    """A refund we counted did not reach the customer: staff must sort it out."""
    payment = _payment_for_intent(refund)
    if payment is not None:
        ledger.flag_attention(ledger.lock_booking(payment.booking_id))
        _fail_refund_entry(payment, refund)


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
    """
    with transaction.atomic():
        try:
            with transaction.atomic():
                StripeEvent.objects.create(event_id=event["id"], type=event["type"])
        except IntegrityError:
            return False
        handler = HANDLERS.get(event["type"])
        if handler is not None:
            handler(event["data"]["object"])
    return True
