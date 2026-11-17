"""Issuing invoices and receipts. Every function here is safe to repeat.

Each document is written while its booking row is locked, with its number
taken from the year's locked sequence row in the same transaction. A
document's source_key names what issued it, so the same booking, payment or
refund never gets a second document however many times the hook runs.
"""

import uuid

from django.db import transaction
from django.utils import timezone

from apps.invoices.models import Invoice
from apps.invoices.services import numbering, snapshot
from apps.payments.models import Payment
from apps.payments.services import ledger

# The parts of a snapshot that, when they change, call for a revised invoice.
MONEY_KEYS = (
    "items",
    "packages",
    "subtotal",
    "discount",
    "delivery_fee",
    "setup_fee",
    "tax",
    "tax_exempt",
    "total",
    "caution_fee",
)
RECEIPTED_STATUSES = (Payment.Status.SUCCEEDED, Payment.Status.REFUNDED)
REFUND_KINDS = (Payment.Kind.RENTAL_REFUND, Payment.Kind.CAUTION_REFUND)


def latest_invoice(booking):
    """The booking's current invoice (not a receipt), or None."""
    return booking.invoices.filter(kind__in=Invoice.INVOICE_KINDS).order_by("-pk").first()


def _issue(
    booking, kind, source_key, *, payment=None, refund=None, note="", by=None, reverses=None
):
    """Write one document for a locked booking, unless source_key already has one.

    Returns (invoice, created).
    """
    existing = Invoice.objects.filter(source_key=source_key).first()
    if existing is not None:
        return existing, False
    data = snapshot.build(booking, payment=payment, refund=refund)
    supersedes = latest_invoice(booking) if kind in Invoice.INVOICE_KINDS else None
    if supersedes is not None:
        data["supersedes"] = supersedes.number
    if reverses is not None:
        data["reverses"] = reverses.number
    if note:
        data["note"] = note
    issued_at = timezone.now()
    invoice = Invoice.objects.create(
        number=numbering.next_number(timezone.localtime(issued_at).year),
        booking=booking,
        kind=kind,
        issued_at=issued_at,
        payment=payment,
        supersedes=supersedes,
        source_key=source_key,
        note=note,
        issued_by=by,
        reverses=reverses,
        snapshot=data,
    )
    return invoice, True


def issue_booking_invoice(booking_id):
    """The first invoice for a booking made online. Returns (invoice, created)."""
    with transaction.atomic():
        booking = ledger.lock_booking(booking_id)
        return _issue(booking, Invoice.Kind.INVOICE, f"booking:{booking_id}")


def issue_receipt(payment_id):
    """A receipt for a succeeded rental payment, or (None, False) if it has not succeeded."""
    with transaction.atomic():
        booking_id = Payment.objects.values_list("booking_id", flat=True).get(pk=payment_id)
        booking = ledger.lock_booking(booking_id)
        payment = Payment.objects.get(pk=payment_id)
        if payment.kind not in Payment.RENTAL_KINDS or payment.status not in RECEIPTED_STATUSES:
            return None, False
        return _issue(booking, Invoice.Kind.RECEIPT, f"receipt:{payment_id}", payment=payment)


def issue_refund_receipt(entry_id):
    """A refund receipt for a succeeded rental or caution refund entry."""
    with transaction.atomic():
        booking_id = Payment.objects.values_list("booking_id", flat=True).get(pk=entry_id)
        booking = ledger.lock_booking(booking_id)
        entry = Payment.objects.get(pk=entry_id)
        if entry.kind not in REFUND_KINDS or entry.status != Payment.Status.SUCCEEDED:
            return None, False
        return _issue(
            booking,
            Invoice.Kind.REFUND_RECEIPT,
            f"refund:{entry_id}",
            payment=entry,
            refund=snapshot.refund_details(entry),
        )


def issue_stripe_refund_receipt(payment_id, refund_id, amount):
    """A refund receipt for rental money refunded at Stripe with no entry of the app's own.

    Refunds made in the Stripe dashboard are counted on the payment they
    refund, with no refund entry; the receipt is keyed on Stripe's refund id.
    """
    with transaction.atomic():
        booking_id = Payment.objects.values_list("booking_id", flat=True).get(pk=payment_id)
        booking = ledger.lock_booking(booking_id)
        payment = Payment.objects.get(pk=payment_id)
        refund = snapshot.refund_details(
            amount=amount,
            method=payment.get_method_display() or "",
            reference=refund_id,
            refund_of=payment.pk,
        )
        return _issue(
            booking,
            Invoice.Kind.REFUND_RECEIPT,
            f"stripe-refund:{refund_id}",
            payment=payment,
            refund=refund,
        )


def _money(data):
    return {key: data.get(key) for key in MONEY_KEYS}


def _reverse(receipt_key, source_key):
    """A refund reversed document for the refund receipt issued under receipt_key, if any."""
    receipt = Invoice.objects.filter(source_key=receipt_key).first()
    if receipt is None:
        return None, False
    with transaction.atomic():
        booking = ledger.lock_booking(receipt.booking_id)
        return _issue(
            booking,
            Invoice.Kind.REFUND_REVERSAL,
            source_key,
            payment=receipt.payment,
            refund=receipt.snapshot.get("refund"),
            reverses=receipt,
        )


def issue_refund_reversal(entry_id):
    """Correct the refund receipt of a refund entry that failed after it had succeeded.

    Returns (None, False) when no refund receipt was issued for it, since the
    customer was then never told the refund was sent.
    """
    return _reverse(f"refund:{entry_id}", f"refund-reversed:{entry_id}")


def issue_stripe_refund_reversal(refund_id):
    """Correct the refund receipt of a Stripe dashboard refund that failed."""
    return _reverse(f"stripe-refund:{refund_id}", f"stripe-refund-reversed:{refund_id}")


def issue_first_invoice(booking_id):
    """The first invoice of a booking that has lines and none yet (made in the admin).

    Shares its source key with the online booking invoice, so a booking never
    gets two first invoices. Returns (None, False) for a booking with no lines.
    """
    with transaction.atomic():
        booking = ledger.lock_booking(booking_id)
        if latest_invoice(booking) is not None or not booking.items.exists():
            return None, False
        return _issue(booking, Invoice.Kind.INVOICE, f"booking:{booking_id}")


def revise_invoice(booking_id, source_key):
    """Issue a revised invoice if the booking's lines or money differ from its current invoice.

    A booking with lines and no invoice yet (one made in the admin) gets its
    first invoice instead. Returns (invoice, created); (None, False) when
    nothing changed.
    """
    with transaction.atomic():
        booking = ledger.lock_booking(booking_id)
        current = latest_invoice(booking)
        if current is None:
            return issue_first_invoice(booking_id)
        existing = Invoice.objects.filter(source_key=source_key).first()
        if existing is not None:
            return existing, False
        if _money(snapshot.build(booking)) == _money(current.snapshot):
            return None, False
        return _issue(booking, Invoice.Kind.INVOICE, source_key)


def issue_manual_invoice(booking, *, note="", by=None):
    """An invoice staff issue for a deal agreed offline: the booking as it stands, plus a note."""
    with transaction.atomic():
        booking = ledger.lock_booking(booking.pk)
        invoice, _ = _issue(
            booking,
            Invoice.Kind.MANUAL_INVOICE,
            f"manual:{uuid.uuid4().hex}",
            note=note.strip(),
            by=by,
        )
    return invoice
