"""Admin form for issuing a manual invoice on a booking."""

from django import forms

from apps.invoices.services import issuing
from apps.payments.forms import LedgerForm


class ManualInvoiceForm(LedgerForm):
    """Issue an invoice for a deal agreed offline: the booking as it stands now, plus a note.

    Set the agreed prices, fees and discount on the booking first; the invoice
    is a snapshot of them and replaces the booking's current invoice.
    """

    title = "Issue manual invoice"

    note = forms.CharField(
        label="Note on the invoice",
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text='Printed on the invoice. For example: "Price agreed by phone on May 2".',
    )

    def record(self, user):
        self.invoice = issuing.issue_manual_invoice(
            self.booking, note=self.cleaned_data["note"], by=user
        )
