"""Admin forms for recording manual payments and caution deposit outcomes."""

from datetime import datetime, time

from django import forms
from django.utils import timezone

from apps.payments.models import Payment
from apps.payments.services import ledger

MANUAL_METHOD_CHOICES = [(m.value, m.label) for m in Payment.MANUAL_METHODS]


def _money_field(label, **kwargs):
    return forms.DecimalField(label=label, max_digits=10, decimal_places=2, min_value=0, **kwargs)


class LedgerForm(forms.Form):
    """Base for forms that write to the ledger and surface its errors."""

    date = forms.DateField(
        label="Date", initial=timezone.localdate, widget=forms.DateInput(attrs={"type": "date"})
    )
    reference = forms.CharField(label="Reference", max_length=255)

    title = ""

    def received_at(self):
        """The chosen date as an aware datetime at local midnight."""
        return timezone.make_aware(datetime.combine(self.cleaned_data["date"], time.min))

    def record(self, booking, user):
        """Write to the ledger. Implemented by subclasses."""
        raise NotImplementedError

    def save(self, booking, user):
        """Record the entry; return it, or None after adding the ledger's error."""
        try:
            return self.record(booking, user)
        except ledger.PaymentError as error:
            self.add_error(None, str(error))
            return None


class ManualPaymentForm(LedgerForm):
    title = "Record manual payment"

    method = forms.ChoiceField(label="Method", choices=MANUAL_METHOD_CHOICES)
    amount = _money_field("Amount towards the rental")
    caution_amount = _money_field("Caution deposit included", required=False, initial=0)

    field_order = ["method", "amount", "caution_amount", "date", "reference"]

    def record(self, booking, user):
        data = self.cleaned_data
        return ledger.record_manual_payment(
            booking,
            method=data["method"],
            amount=data["amount"],
            caution_amount=data["caution_amount"] or 0,
            received_at=self.received_at(),
            reference=data["reference"],
            received_by=user,
        )


class CautionRefundForm(LedgerForm):
    title = "Record caution refund"

    method = forms.ChoiceField(label="Refunded by", choices=Payment.Method.choices)
    amount = _money_field("Amount refunded")

    field_order = ["method", "amount", "date", "reference"]

    def record(self, booking, user):
        data = self.cleaned_data
        return ledger.record_caution_refund(
            booking,
            amount=data["amount"],
            method=data["method"],
            reference=data["reference"],
            received_at=self.received_at(),
            received_by=user,
        )


class DamageChargeForm(LedgerForm):
    title = "Record damage charge"

    amount = _money_field("Amount kept from the caution deposit")

    field_order = ["amount", "date", "reference"]

    def record(self, booking, user):
        data = self.cleaned_data
        return ledger.record_damage_charge(
            booking,
            amount=data["amount"],
            reference=data["reference"],
            received_at=self.received_at(),
            received_by=user,
        )
