"""Shared helpers for payment tests: bookings with money on them and signed webhooks."""

import hashlib
import hmac
import itertools
import json
import time
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.tests.factories import BookingFactory, BookingItemFactory
from apps.payments.services import stripe as stripe_service

WEBHOOK_SECRET = "whsec_test_secret"  # noqa: S105
URLS = {"success_url": "https://example.test/ok", "cancel_url": "https://example.test/back"}

_sequence = itertools.count(1)


def held_booking(total="200.00", caution_fee="0.00", **kwargs):
    """A booking on a live hold with one item line worth total."""
    booking = BookingFactory(
        status=Booking.Status.HELD,
        hold_expires_at=timezone.now() + timedelta(minutes=15),
        caution_fee=Decimal(caution_fee),
        **kwargs,
    )
    BookingItemFactory(booking=booking, quantity=1, unit_price=Decimal(total))
    return booking


def start_checkout(booking, kind):
    """Create a Checkout session with Stripe mocked. Returns (payment, create kwargs)."""
    session_id = f"cs_test_{next(_sequence)}"
    with mock.patch.object(stripe_service.stripe.checkout, "Session") as session_api:
        session_api.create.return_value = {"id": session_id, "url": "https://stripe.test/pay"}
        stripe_service.create_checkout_session(booking, kind, **URLS)
        create_kwargs = session_api.create.call_args.kwargs
    payment = booking.payments.get(stripe_checkout_session_id=session_id)
    return payment, create_kwargs


def session_object(payment, payment_status="paid", amount_total=None):
    """The Checkout session Stripe would send for one of our payments."""
    if amount_total is None:
        amount_total = stripe_service.to_cents(payment.charged_total)
    return {
        "id": payment.stripe_checkout_session_id,
        "object": "checkout.session",
        "amount_total": amount_total,
        "currency": "usd",
        "payment_status": payment_status,
        "payment_intent": f"pi_{payment.stripe_checkout_session_id}",
        "metadata": {
            "booking_id": str(payment.booking_id),
            "kind": payment.kind,
            "payment_id": str(payment.pk),
        },
    }


class FakeStripeRefunds:
    """Stands in for stripe.Refund: remembers refunds and lists them by payment intent."""

    def __init__(self):
        self.refunds = []
        self.create_calls = []

    def add(self, payment, amount, kind=None, status="succeeded"):
        """Add a refund as Stripe would hold it. kind None is a dashboard refund."""
        refund = {
            "id": f"re_test_{next(_sequence)}",
            "object": "refund",
            "payment_intent": payment.stripe_payment_intent_id,
            "amount": stripe_service.to_cents(Decimal(amount)),
            "status": status,
            "metadata": {"kind": kind} if kind else {},
        }
        self.refunds.append(refund)
        return refund

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        refund = {
            "id": f"re_test_{next(_sequence)}",
            "object": "refund",
            "payment_intent": kwargs["payment_intent"],
            "amount": kwargs["amount"],
            "status": "succeeded",
            "metadata": kwargs["metadata"],
        }
        self.refunds.append(refund)
        return refund

    def list(self, **kwargs):
        intent = kwargs["payment_intent"]
        return {"data": [r for r in self.refunds if r["payment_intent"] == intent]}

    def total_cents(self, payment):
        return sum(
            r["amount"]
            for r in self.list(payment_intent=payment.stripe_payment_intent_id)["data"]
            if r["status"] not in ("failed", "canceled")
        )


def charge_refunded_event(payment, refunds):
    """The charge.refunded event Stripe sends once refunds exist on a payment."""
    charge = {
        "id": f"ch_{payment.pk}",
        "object": "charge",
        "payment_intent": payment.stripe_payment_intent_id,
        "amount": stripe_service.to_cents(payment.charged_total),
        "amount_refunded": refunds.total_cents(payment),
    }
    return event("charge.refunded", charge)


def event(event_type, data_object, event_id=None):
    return {
        "id": event_id or f"evt_test_{next(_sequence)}",
        "type": event_type,
        "data": {"object": data_object},
    }


def post_event(client, payload, secret=WEBHOOK_SECRET, signature=None):
    """POST an event to the webhook, signed the way Stripe signs it."""
    body = json.dumps(payload)
    timestamp = int(time.time())
    if signature is None:
        signature = hmac.new(
            secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256
        ).hexdigest()
    return client.post(
        reverse("payments:stripe_webhook"),
        data=body,
        content_type="application/json",
        headers={"Stripe-Signature": f"t={timestamp},v1={signature}"},
    )


def pay(client, booking, kind, payment_status="paid"):
    """Run a whole successful card checkout: session, then the completed webhook."""
    payment, _ = start_checkout(booking, kind)
    response = post_event(
        client, event("checkout.session.completed", session_object(payment, payment_status))
    )
    assert response.status_code == 200
    payment.refresh_from_db()
    booking.refresh_from_db()
    return payment
