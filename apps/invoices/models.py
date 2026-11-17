"""Invoices and receipts: numbered, immutable snapshots of a booking's money at issue time."""

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class InvoiceImmutable(Exception):
    """Raised when something tries to change or delete an issued document."""


class InvoiceSequence(TimeStampedModel):
    """The last number issued in a year. Locked while a number is taken, so none is skipped."""

    year = models.PositiveIntegerField(unique=True)
    last_number = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.year}: {self.last_number}"


class InvoiceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise InvoiceImmutable("Issued invoices cannot be changed.")

    def delete(self):
        raise InvoiceImmutable("Issued invoices cannot be deleted.")


class Invoice(TimeStampedModel):
    """One issued document. Never edited: a change issues a new one that supersedes it.

    snapshot holds every line and amount as they stood when it was issued, as
    strings, and the PDF is always rendered from it. source_key names the event
    that issued it (a booking, a payment, a refund), so a repeated event or a
    retried task can never issue a second document for it.
    """

    class Kind(models.TextChoices):
        INVOICE = "invoice", "Invoice"
        RECEIPT = "receipt", "Receipt"
        REFUND_RECEIPT = "refund_receipt", "Refund receipt"
        MANUAL_INVOICE = "manual_invoice", "Manual invoice"
        REFUND_REVERSAL = "refund_reversal", "Refund reversed"

    INVOICE_KINDS = (Kind.INVOICE, Kind.MANUAL_INVOICE)

    number = models.CharField(max_length=20, unique=True)
    booking = models.ForeignKey(
        "bookings.Booking", on_delete=models.PROTECT, related_name="invoices"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    issued_at = models.DateTimeField()
    payment = models.ForeignKey(
        "payments.Payment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="invoices",
        help_text="The payment a receipt is for, or the refund entry a refund receipt is for.",
    )
    supersedes = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="superseded_by",
        help_text="The earlier invoice this one replaces.",
    )
    reverses = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_by",
        help_text="The refund receipt a refund reversed document corrects.",
    )
    source_key = models.CharField(max_length=120, unique=True)
    note = models.TextField(blank=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="invoices_issued",
    )
    snapshot = models.JSONField()

    objects = InvoiceQuerySet.as_manager()

    class Meta:
        ordering = ["-issued_at", "-pk"]

    def __str__(self):
        return f"{self.get_kind_display()} {self.number}"

    def save(self, *args, **kwargs):
        """Write a new document once; an issued one is never written again."""
        if self.pk is not None:
            raise InvoiceImmutable("Issued invoices cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise InvoiceImmutable("Issued invoices cannot be deleted.")

    @property
    def filename(self):
        return f"{self.number}.pdf"
