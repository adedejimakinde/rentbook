"""What the booking and ledger services call when a document is due.

Each hook queues a Celery task to run once the caller's transaction commits,
so nothing is issued or emailed for changes that roll back. The tasks are
idempotent, so a hook that runs twice for the same event issues one document.
"""

import uuid

from apps.core.queue import enqueue_on_commit


def _after_commit(task_name, *args):
    from apps.invoices import tasks

    enqueue_on_commit(getattr(tasks, task_name), *args)


def booking_created(booking):
    """A booking was made online: issue its invoice and send the booking received emails."""
    _after_commit("issue_booking_invoice", booking.pk)


def first_invoice(booking):
    """A booking made in the admin may have its first lines: invoice it, without an email."""
    _after_commit("issue_first_invoice", booking.pk)


def payment_succeeded(payment, *, confirmed):
    """Rental money arrived. confirmed: this payment secured the booking."""
    _after_commit("issue_payment_documents", payment.pk, confirmed)


def refund_succeeded(entry):
    """A rental or caution refund entry succeeded."""
    _after_commit("issue_refund_documents", entry.pk)


def stripe_refund_counted(payment, refund_id, amount):
    """Rental money was refunded at Stripe with no refund entry (from the Stripe dashboard)."""
    from apps.invoices.models import Invoice

    if Invoice.objects.filter(source_key=f"stripe-refund:{refund_id}").exists():
        return
    _after_commit("issue_stripe_refund_documents", payment.pk, refund_id, str(amount))


def refund_reversed(entry):
    """A refund entry that had succeeded failed at Stripe: correct its refund receipt."""
    _after_commit("issue_refund_reversal_documents", entry.pk)


def stripe_refund_reversed(refund_id):
    """A Stripe dashboard refund that had been counted failed: correct its refund receipt."""
    _after_commit("issue_stripe_refund_reversal_documents", refund_id)


def booking_changed(booking):
    """A booking's lines or money may have changed: revise its invoice if they did."""
    _after_commit("revise_invoice", booking.pk, f"revision:{uuid.uuid4().hex}")
