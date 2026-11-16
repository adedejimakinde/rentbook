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
# Stripe's shortest allowed session lifetime is 30 minutes.
CHECKOUT_SESSION_MINUTES = 31
HOLD_GRACE_MINUTES = 5


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


def _close_open_sessions(booking):
    """Expire the booking's unfinished Checkout sessions so it cannot be paid twice.

    Raises PaymentError when one of them is already paid or clearing.
    """
    pending = booking.payments.filter(status=Payment.Status.PENDING, kind__in=Payment.RENTAL_KINDS)
    for payment in pending:
        session_id = payment.stripe_checkout_session_id
        if session_id:
            try:
                stripe.checkout.Session.expire(session_id, api_key=_secret_key())
            except stripe.InvalidRequestError:
                session = stripe.checkout.Session.retrieve(session_id, api_key=_secret_key())
                if session["status"] != "expired":
                    raise PaymentError(
                        "A payment for this booking is already being processed."
                    ) from None
        ledger.record_failure(payment.pk)


def _open_payment(booking_id, kind, session_expires_at):
    """Work out the amount owed for kind and create the pending payment, under lock."""
    with transaction.atomic():
        booking = ledger.lock_booking(booking_id)
        on_hold = ledger.has_live_hold(booking)
        if not (on_hold or booking.status == Booking.Status.DEPOSIT_PAID):
            raise PaymentError("This booking is not open for payment.")

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
        if kind != Payment.Kind.DEPOSIT:
            caution = max(booking.caution_fee - booking.payments.caution_collected(), ZERO)

        if on_hold:
            keep_until = session_expires_at + timedelta(minutes=HOLD_GRACE_MINUTES)
            booking.hold_expires_at = max(booking.hold_expires_at, keep_until)
            booking.save(update_fields=["hold_expires_at", "updated_at"])
        return Payment.objects.create(
            booking=booking,
            amount=amount,
            caution_amount=caution,
            method=Payment.Method.CARD,
            kind=kind,
        )


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
    """
    api_key = _secret_key()
    _close_open_sessions(booking)
    expires_at = timezone.now() + timedelta(minutes=CHECKOUT_SESSION_MINUTES)
    payment = _open_payment(booking.pk, kind, expires_at)

    label = f"Booking #{booking.pk} for {booking.event_date:%b %d, %Y}"
    line_items = [_line_item(f"{payment.get_kind_display()}: {label}", payment.amount)]
    if payment.caution_amount > ZERO:
        line_items.append(
            _line_item(f"Refundable caution deposit: {label}", payment.caution_amount)
        )
    metadata = {
        "booking_id": str(booking.pk),
        "kind": str(kind),
        "payment_id": str(payment.pk),
    }
    try:
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
    except stripe.StripeError:
        ledger.record_failure(payment.pk)
        raise
    Payment.objects.filter(pk=payment.pk).update(
        stripe_checkout_session_id=session["id"], updated_at=timezone.now()
    )
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


def _on_charge_refunded(charge):
    intent_id = charge.get("payment_intent")
    payment = (
        Payment.objects.filter(stripe_payment_intent_id=intent_id).first() if intent_id else None
    )
    if payment is None:
        logger.warning("Refunded Stripe charge %s matches no payment.", charge["id"])
        return
    ledger.record_refund(payment.pk, from_cents(charge["amount_refunded"]))


HANDLERS = {
    "checkout.session.completed": _on_checkout_completed,
    "checkout.session.async_payment_succeeded": _on_async_payment_succeeded,
    "checkout.session.async_payment_failed": _on_async_payment_failed,
    "charge.refunded": _on_charge_refunded,
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
