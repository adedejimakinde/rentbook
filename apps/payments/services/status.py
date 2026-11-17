"""What a customer sees after Stripe Checkout. Read only: nothing here records money.

A booking is marked paid only by the verified webhook (stripe.handle_event).
These helpers just report what the webhook has recorded so far.
"""

from apps.bookings.models import Booking
from apps.payments.models import Payment
from apps.payments.services import ledger

CONFIRMING = "confirming"
PROCESSING = "processing"
CONFIRMED = "confirmed"
FAILED = "failed"
FINAL_STATES = (PROCESSING, CONFIRMED, FAILED)


def checkout_state(booking, session_id=""):
    """CONFIRMING, PROCESSING (ACH clearing), CONFIRMED or FAILED for a checkout.

    With the Checkout session id Stripe adds to the success URL, the state is
    that payment's. Without it, the booking's own status decides: secured or
    fulfilled means confirmed, anything else is still being confirmed.
    """
    payment = None
    if session_id:
        payment = booking.payments.filter(stripe_checkout_session_id=session_id).first()
    if payment is not None:
        if payment.status in (Payment.Status.SUCCEEDED, Payment.Status.REFUNDED):
            return CONFIRMED
        if payment.status == Payment.Status.FAILED:
            return FAILED
        if payment.method == Payment.Method.ACH:
            return PROCESSING
        return CONFIRMING
    if booking.status in (*ledger.SECURED_STATUSES, *ledger.FULFILLED_STATUSES):
        return CONFIRMED
    return CONFIRMING


def booking_is_secured(booking):
    return booking.status in (*ledger.SECURED_STATUSES, *ledger.FULFILLED_STATUSES)


def status_summary(booking):
    """Items, packages and money for the booking status page, in few queries."""
    booking = Booking.objects.prefetch_related("items__variant__item", "packages").get(
        pk=booking.pk
    )
    items = [
        {"name": str(item.variant), "quantity": item.quantity - item.package_quantity}
        for item in booking.items.all()
        if item.quantity > item.package_quantity
    ]
    total = booking.total
    paid = booking.amount_paid
    return {
        "booking": booking,
        "items": items,
        "packages": list(booking.packages.all()),
        "total": total,
        "paid": paid,
        "balance_due": max(total - paid, 0),
        "caution_fee": booking.caution_fee,
        "caution_held": booking.caution_held,
        "caution_due": booking.caution_due,
        "in_progress": booking.payments.open_checkouts().exists(),
    }
