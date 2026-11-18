"""Admin forms for moving a booking through its lifecycle and changing its lines."""

from django import forms

from apps.bookings.services import lifecycle
from apps.catalogue.models import ItemVariant
from apps.payments.forms import LedgerForm


def _clearing_override_field():
    return forms.CharField(
        label="Override note (ACH clearing)",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=(
            "Only needed while an ACH bank payment is still clearing. Say why you are going "
            "ahead; the note, your name and the time are recorded and the booking is flagged."
        ),
    )


class CancelBookingForm(LedgerForm):
    title = "Cancel booking"

    override_note = _clearing_override_field()

    def record(self, user):
        lifecycle.cancel_booking(
            self.booking, by=user, override_note=self.cleaned_data["override_note"]
        )


class ReopenBookingForm(LedgerForm):
    title = "Reopen booking"

    def record(self, user):
        lifecycle.reopen_booking(self.booking)


class MarkDeliveredForm(LedgerForm):
    title = "Mark delivered"

    override_note = forms.CharField(
        label="Override note",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=(
            "Only needed to deliver with a balance owed, caution not collected or a "
            'payment in progress. For example: "Balance collected on delivery".'
        ),
    )

    def record(self, user):
        lifecycle.mark_delivered(
            self.booking, by=user, override_note=self.cleaned_data["override_note"]
        )


class MarkReturnedForm(LedgerForm):
    title = "Mark returned"

    def record(self, user):
        lifecycle.mark_returned(self.booking)


class ChangeItemForm(LedgerForm):
    """Change one item line. Used once a booking has payments and its lines are locked."""

    title = "Change item line"

    variant = forms.ModelChoiceField(
        label="Item", queryset=ItemVariant.objects.select_related("item")
    )
    quantity = forms.IntegerField(
        label="New quantity", min_value=0, help_text="Zero removes the line."
    )
    unit_price = forms.DecimalField(
        label="Unit price",
        max_digits=10,
        decimal_places=2,
        min_value=0,
        required=False,
        help_text="Leave empty to keep the current price (or use the catalogue price).",
    )
    override_note = _clearing_override_field()

    def record(self, user):
        data = self.cleaned_data
        lifecycle.change_item(
            self.booking,
            data["variant"],
            quantity=data["quantity"],
            unit_price=data["unit_price"],
            by=user,
            override_note=data["override_note"],
        )


class TaxExemptionForm(LedgerForm):
    """Mark a booking tax exempt, with its certificate, or taxable again."""

    title = "Set tax exemption"

    tax_exempt = forms.BooleanField(label="Tax exempt", required=False)
    certificate = forms.CharField(
        label="Exemption certificate",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Certificate number and issuing state or authority. Required when exempt.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tax_exempt"].initial = self.booking.tax_exempt
        self.fields["certificate"].initial = self.booking.tax_exempt_certificate

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("tax_exempt") and not (cleaned.get("certificate") or "").strip():
            self.add_error("certificate", "Enter the exemption certificate.")
        return cleaned

    def record(self, user):
        data = self.cleaned_data
        lifecycle.set_tax_exemption(
            self.booking, exempt=data["tax_exempt"], certificate=data["certificate"], by=user
        )


class ResolveAttentionForm(LedgerForm):
    """Clear a booking's needs-attention flag, with a note saying how it was resolved."""

    title = "Resolve attention"

    note = forms.CharField(
        label="How was it resolved?",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Saved in the booking's log with your name and the time.",
    )

    def record(self, user):
        lifecycle.resolve_attention(self.booking, self.cleaned_data["note"], user)


LIFECYCLE_FORMS = {
    "cancel": CancelBookingForm,
    "reopen": ReopenBookingForm,
    "mark-delivered": MarkDeliveredForm,
    "mark-returned": MarkReturnedForm,
    "change-item": ChangeItemForm,
    "tax-exemption": TaxExemptionForm,
    "resolve-attention": ResolveAttentionForm,
}
