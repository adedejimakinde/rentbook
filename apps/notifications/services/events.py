"""Texts that go with the booking and payment emails: who they are for and their keys."""

from apps.bookings.models import Booking
from apps.invoices.models import Invoice
from apps.notifications.services import dispatch, messages


def _from_receipt(receipt_id):
    receipt = Invoice.objects.select_related("booking").get(pk=receipt_id)
    return receipt.booking, str(receipt.payment_id), receipt.snapshot.get("payment", {})


def queue_texts(kind, object_id):
    """Queue the SMS and WhatsApp messages for one booking or payment event."""
    if kind in ("booking_received", "owner_new_booking"):
        booking = Booking.objects.get(pk=object_id)
        suffix, extra = "", {}
    elif kind in ("booking_confirmed", "owner_payment"):
        booking, suffix, payment = _from_receipt(object_id)
        extra = {"amount": payment.get("total", "")}
    else:
        raise ValueError(f"No texts for {kind!r}")
    if not messages.still_applies(kind, booking):
        return False
    return dispatch.queue(booking, kind, suffix, email=False, extra=extra)
