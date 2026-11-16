"""Admin forms for recording manual payments and caution deposit outcomes."""

from datetime import datetime, time

import stripe
from django import forms
from django.utils import timezone

from apps.payments.models import Payment
from apps.payments.services import ledger
from apps.payments.services import stripe as stripe_service

MANUAL_METHOD_CHOICES = [(m.value, m.label) for m in Payment.MANUAL_METHODS]
VIA_STRIPE = "stripe"


def _money_field(label, **kwargs):
    return forms.DecimalField(label=label, max_digits=10, decimal_places=2, min_value=0, **kwargs)


class LedgerForm(forms.Form):
    """Base for forms that write to the ledger and surface its errors."""

    title = ""

    def __init__(self, *args, booking, **kwargs):
        super().__init__(*args, **kwargs)
        self.booking = booking

    def record(self, user):
        """Write to the ledger. Implemented by subclasses."""
        raise NotImplementedError

    def save(self, user):
        """Record the entry; return True, or False after adding the error to the form."""
        try:
            self.record(user)
        except ledger.PaymentError as error:
            self.add_error(None, str(error))
        except stripe.StripeError as error:
            self.add_error(None, f"Stripe could not do this: {error.user_message or error}")
        else:
            return True
        return False


class DatedLedgerForm(LedgerForm):
    """A ledger form for money staff moved themselves, with its date and reference."""

    date = forms.DateField(
        label="Date", initial=timezone.localdate, widget=forms.DateInput(attrs={"type": "date"})
    )
    reference = forms.CharField(label="Reference", max_length=255, required=False)

    reference_required = True

    def clean(self):
        cleaned = super().clean()
        if self.reference_required and not cleaned.get("reference"):
            self.add_error("reference", "Enter the payment reference.")
        return cleaned

    def received_at(self):
        """The chosen date as an aware datetime at local midnight."""
        return timezone.make_aware(datetime.combine(self.cleaned_data["date"], time.min))


class ManualPaymentForm(DatedLedgerForm):
    title = "Record manual payment"

    method = forms.ChoiceField(label="Method", choices=MANUAL_METHOD_CHOICES)
    amount = _money_field("Amount towards the rental")
    caution_amount = _money_field("Caution deposit included", required=False, initial=0)

    field_order = ["method", "amount", "caution_amount", "date", "reference"]

    def record(self, user):
        data = self.cleaned_data
        stripe_service.close_open_checkouts(self.booking)
        ledger.record_manual_payment(
            self.booking,
            method=data["method"],
            amount=data["amount"],
            caution_amount=data["caution_amount"] or 0,
            received_at=self.received_at(),
            reference=data["reference"],
            received_by=user,
        )


class CautionRefundForm(DatedLedgerForm):
    """Refund caution money: through Stripe to the original card or bank, or manually."""

    title = "Record caution refund"

    method = forms.ChoiceField(
        label="Refund by",
        choices=[
            (VIA_STRIPE, "Original card or bank account (sent through Stripe now)"),
            *MANUAL_METHOD_CHOICES,
        ],
    )
    amount = _money_field("Amount refunded")

    field_order = ["method", "amount", "date", "reference"]

    @property
    def reference_required(self):
        return self.cleaned_data.get("method") != VIA_STRIPE

    def record(self, user):
        data = self.cleaned_data
        if data["method"] == VIA_STRIPE:
            stripe_service.refund_caution(self.booking, data["amount"], received_by=user)
            return
        ledger.record_caution_refund(
            self.booking,
            amount=data["amount"],
            method=data["method"],
            reference=data["reference"],
            received_at=self.received_at(),
            received_by=user,
        )


class DamageChargeForm(DatedLedgerForm):
    title = "Record damage charge"

    amount = _money_field("Amount kept from the caution deposit")

    field_order = ["amount", "date", "reference"]

    def record(self, user):
        data = self.cleaned_data
        ledger.record_damage_charge(
            self.booking,
            amount=data["amount"],
            reference=data["reference"],
            received_at=self.received_at(),
            received_by=user,
        )


class PaymentChoiceField(forms.ModelChoiceField):
    """Lists payments with what can still be refunded on each."""

    def label_from_instance(self, payment):
        return (
            f"{payment.get_kind_display()} by {payment.get_method_display()}: "
            f"${payment.amount} (refundable ${payment.rental_refundable})"
        )


class RentalRefundForm(DatedLedgerForm):
    """Refund rental money on one of the booking's payments.

    A card or ACH payment is refunded through Stripe. A manual payment is
    refunded by staff, who record how, when and the reference here.
    """

    title = "Refund rental payment"

    payment = PaymentChoiceField(label="Payment to refund", queryset=Payment.objects.none())
    amount = _money_field("Amount to refund")
    method = forms.ChoiceField(
        label="Sent back by (manual payments only)",
        choices=[("", "---------"), *MANUAL_METHOD_CHOICES],
        required=False,
    )

    field_order = ["payment", "amount", "method", "date", "reference"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["payment"].queryset = self.booking.payments.filter(
            kind__in=Payment.RENTAL_KINDS,
            status__in=(Payment.Status.SUCCEEDED, Payment.Status.REFUNDED),
        )
        self.fields["date"].label = "Date sent back (manual payments only)"
        self.fields["date"].required = False
        self.fields["reference"].label = "Reference (manual payments only)"

    @property
    def is_manual(self):
        payment = self.cleaned_data.get("payment")
        return payment is not None and not payment.stripe_payment_intent_id

    @property
    def reference_required(self):
        return self.is_manual

    def clean(self):
        cleaned = super().clean()
        if self.is_manual and not cleaned.get("method"):
            self.add_error("method", "Choose how the refund was sent back.")
        if self.is_manual and not cleaned.get("date") and "date" not in self.errors:
            self.add_error("date", "Enter the date the refund was sent back.")
        return cleaned

    def record(self, user):
        data = self.cleaned_data
        if not self.is_manual:
            stripe_service.refund_rental(data["payment"], data["amount"], received_by=user)
            return
        ledger.record_manual_rental_refund(
            data["payment"],
            amount=data["amount"],
            method=data["method"],
            reference=data["reference"],
            received_at=self.received_at(),
            received_by=user,
        )
