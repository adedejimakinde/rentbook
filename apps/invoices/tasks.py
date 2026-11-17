"""Background jobs that issue documents, then queue the emails that go with them."""

from decimal import Decimal

from celery import shared_task

from apps.invoices.services import issuing, sweep
from apps.notifications import hooks as notify

RETRY = {
    "autoretry_for": (Exception,),
    "retry_backoff": True,
    "retry_backoff_max": 600,
    "max_retries": 5,
    "ignore_result": True,
}


@shared_task(name="invoices.issue_booking_invoice", **RETRY)
def issue_booking_invoice(booking_id):
    """Invoice a new online booking; tell the customer and the owner."""
    issuing.issue_booking_invoice(booking_id)
    notify.booking_received(booking_id)
    notify.owner_new_booking(booking_id)


@shared_task(name="invoices.issue_first_invoice", **RETRY)
def issue_first_invoice(booking_id):
    """First invoice for a booking made in the admin. Not emailed; staff send it."""
    issuing.issue_first_invoice(booking_id)


@shared_task(name="invoices.issue_missing_documents", ignore_result=True)
def issue_missing_documents():
    """Issue documents whose job was lost, and queue their emails. Every 15 minutes."""
    return sweep.issue_missing()


@shared_task(name="invoices.issue_payment_documents", **RETRY)
def issue_payment_documents(payment_id, confirmed):
    """Receipt a payment; send the confirmation (or the receipt) and tell the owner."""
    receipt, _ = issuing.issue_receipt(payment_id)
    if receipt is None:
        return
    if confirmed:
        notify.booking_confirmed(receipt.pk)
    else:
        notify.payment_receipt(receipt.pk)
    notify.owner_payment(receipt.pk)


@shared_task(name="invoices.issue_refund_documents", **RETRY)
def issue_refund_documents(entry_id):
    """Refund receipt for a refund entry, emailed to the customer."""
    receipt, _ = issuing.issue_refund_receipt(entry_id)
    if receipt is not None:
        notify.refund_receipt(receipt.pk)


@shared_task(name="invoices.issue_stripe_refund_documents", **RETRY)
def issue_stripe_refund_documents(payment_id, refund_id, amount):
    """Refund receipt for a refund made in the Stripe dashboard, emailed to the customer."""
    receipt, _ = issuing.issue_stripe_refund_receipt(payment_id, refund_id, Decimal(amount))
    notify.refund_receipt(receipt.pk)


@shared_task(name="invoices.issue_refund_reversal_documents", **RETRY)
def issue_refund_reversal_documents(entry_id):
    """Refund reversed document for a refund receipt whose refund failed, emailed."""
    reversal, _ = issuing.issue_refund_reversal(entry_id)
    if reversal is not None:
        notify.refund_reversed(reversal.pk)


@shared_task(name="invoices.issue_stripe_refund_reversal_documents", **RETRY)
def issue_stripe_refund_reversal_documents(refund_id):
    """Refund reversed document for a failed Stripe dashboard refund, emailed."""
    reversal, _ = issuing.issue_stripe_refund_reversal(refund_id)
    if reversal is not None:
        notify.refund_reversed(reversal.pk)


@shared_task(name="invoices.revise_invoice", **RETRY)
def revise_invoice(booking_id, source_key):
    """Issue a revised invoice if the booking's money changed."""
    issuing.revise_invoice(booking_id, source_key)
