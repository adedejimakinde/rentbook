"""Catch-up for documents whose job was lost (for example the broker was down at commit).

Runs every 15 minutes from Celery Beat. It looks only at rows changed in the
last LOOKBACK, so turning it on never back-fills old history (and emails old
customers), and skips rows changed in the last MIN_AGE, which the normal
on-commit jobs are still handling. Everything it queues is idempotent, so a
row the normal job already finished is left as it is.
"""

from datetime import timedelta

from django.utils import timezone

from apps.bookings.models import Booking
from apps.invoices import hooks as documents
from apps.invoices.models import Invoice
from apps.payments.models import Payment
from apps.payments.services import ledger

MIN_AGE = timedelta(minutes=5)
LOOKBACK = timedelta(days=3)
BATCH = 200
RECEIPTED = (Payment.Status.SUCCEEDED, Payment.Status.REFUNDED)
REFUND_KINDS = (Payment.Kind.RENTAL_REFUND, Payment.Kind.CAUTION_REFUND)


def _window(now):
    return (now - LOOKBACK, now - MIN_AGE)


def payments_without_receipt(now):
    return (
        Payment.objects.filter(
            kind__in=Payment.RENTAL_KINDS, status__in=RECEIPTED, updated_at__range=_window(now)
        )
        .exclude(invoices__kind=Invoice.Kind.RECEIPT)
        .select_related("booking")
        .order_by("pk")[:BATCH]
    )


def refunds_without_receipt(now):
    return (
        Payment.objects.filter(
            kind__in=REFUND_KINDS, status=Payment.Status.SUCCEEDED, updated_at__range=_window(now)
        )
        .exclude(invoices__kind=Invoice.Kind.REFUND_RECEIPT)
        .order_by("pk")[:BATCH]
    )


def bookings_without_invoice(now):
    """Bookings with lines and no invoice: made online, or made in the admin."""
    return (
        Booking.objects.filter(updated_at__range=_window(now), items__isnull=False)
        .exclude(invoices__kind__in=Invoice.INVOICE_KINDS)
        .distinct()
        .order_by("pk")[:BATCH]
    )


def _confirmed(payment):
    """Whether this payment is the one that secured its booking: its first receipted payment."""
    booking = payment.booking
    if booking.status not in (*ledger.SECURED_STATUSES, *ledger.FULFILLED_STATUSES):
        return False
    earlier = booking.payments.filter(
        kind__in=Payment.RENTAL_KINDS, status__in=RECEIPTED, pk__lt=payment.pk
    )
    return not earlier.exists()


def issue_missing(now=None):
    """Queue the jobs for every document still missing. Returns how many of each were queued."""
    now = now or timezone.now()
    counts = {"invoices": 0, "receipts": 0, "refund_receipts": 0}
    for booking in bookings_without_invoice(now):
        if booking.terms_accepted_at is not None:
            documents.booking_created(booking)
        else:
            documents.first_invoice(booking)
        counts["invoices"] += 1
    for payment in payments_without_receipt(now):
        documents.payment_succeeded(payment, confirmed=_confirmed(payment))
        counts["receipts"] += 1
    for entry in refunds_without_receipt(now):
        documents.refund_succeeded(entry)
        counts["refund_receipts"] += 1
    return counts
