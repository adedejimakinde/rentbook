"""Admin forms for moving a booking through its lifecycle and changing its lines."""

from django import forms

from apps.bookings.services import lifecycle
from apps.catalogue.models import ItemVariant
from apps.payments.forms import LedgerForm


class CancelBookingForm(LedgerForm):
    title = "Cancel booking"

    def record(self, user):
        lifecycle.cancel_booking(self.booking)


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

    def record(self, user):
        data = self.cleaned_data
        lifecycle.change_item(
            self.booking,
            data["variant"],
            quantity=data["quantity"],
            unit_price=data["unit_price"],
        )


LIFECYCLE_FORMS = {
    "cancel": CancelBookingForm,
    "reopen": ReopenBookingForm,
    "mark-delivered": MarkDeliveredForm,
    "mark-returned": MarkReturnedForm,
    "change-item": ChangeItemForm,
}
