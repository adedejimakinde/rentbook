"""Forms for the public booking flow, one per step. They validate input only, never price."""

import re

from django import forms
from django.urls import reverse
from django.utils.safestring import mark_safe

from apps.bookings.models import EventType, PaymentChoice, PaymentPlan, Surface, normalise_zip
from apps.bookings.services.availability import notice_problem
from apps.bookings.services.quote import zone_for_zip
from apps.core.forms import INPUT

CHOICE_INPUT = "h-5 w-5 accent-primary"


class EventForm(forms.Form):
    event_date = forms.DateField(widget=forms.DateInput(attrs={"class": INPUT, "type": "date"}))
    start_time = forms.TimeField(
        label="Event start time", widget=forms.TimeInput(attrs={"class": INPUT, "type": "time"})
    )
    setup_by_time = forms.TimeField(
        label="Setup finished by",
        help_text="When everything needs to be ready. Usually an hour before the start.",
        widget=forms.TimeInput(attrs={"class": INPUT, "type": "time"}),
    )
    event_type = forms.ChoiceField(
        choices=[("", "Choose one"), *EventType.choices],
        widget=forms.Select(attrs={"class": INPUT}),
    )

    def clean_event_date(self):
        event_date = self.cleaned_data["event_date"]
        problem = notice_problem(event_date)
        if problem:
            raise forms.ValidationError(problem)
        return event_date

    def clean(self):
        cleaned = super().clean()
        start, setup_by = cleaned.get("start_time"), cleaned.get("setup_by_time")
        if start and setup_by and setup_by > start:
            self.add_error("setup_by_time", "Setup must be finished by the event start time.")
        return cleaned

    def to_session(self):
        data = self.cleaned_data
        return {
            "event_date": data["event_date"].isoformat(),
            "start_time": data["start_time"].strftime("%H:%M"),
            "setup_by_time": data["setup_by_time"].strftime("%H:%M"),
            "event_type": data["event_type"],
        }


class DeliveryForm(forms.Form):
    street = forms.CharField(
        label="Street address",
        max_length=200,
        widget=forms.TextInput(attrs={"class": INPUT, "autocomplete": "street-address"}),
    )
    city = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={"class": INPUT, "autocomplete": "address-level2"}),
    )
    state = forms.CharField(
        max_length=2,
        initial="TX",
        widget=forms.TextInput(attrs={"class": INPUT, "autocomplete": "address-level1"}),
    )
    zip_code = forms.CharField(
        label="ZIP code",
        max_length=10,
        widget=forms.TextInput(
            attrs={"class": INPUT, "autocomplete": "postal-code", "inputmode": "numeric"}
        ),
    )
    surface = forms.ChoiceField(
        label="Setup surface",
        choices=Surface.choices,
        widget=forms.RadioSelect(attrs={"class": CHOICE_INPUT}),
    )
    crew_notes = forms.CharField(
        label="Notes for the crew (optional)",
        required=False,
        max_length=1000,
        help_text="Gate codes, parking, stairs, where to set up, power outlets.",
        widget=forms.Textarea(attrs={"class": INPUT, "rows": 3}),
    )

    def clean_state(self):
        state = self.cleaned_data["state"].strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", state):
            raise forms.ValidationError("Enter the two-letter state, for example TX.")
        return state

    def clean_zip_code(self):
        raw = self.cleaned_data["zip_code"]
        zip_code = normalise_zip(raw)
        if not zip_code:
            raise forms.ValidationError("Enter a 5-digit ZIP code.")
        if zone_for_zip(zip_code) is None:
            raise forms.ValidationError(
                f"We do not deliver to ZIP code {zip_code} yet. Call or WhatsApp us and we "
                "will see what we can do."
            )
        return zip_code

    def to_session(self):
        data = self.cleaned_data
        return {key: data[key] for key in ("street", "city", "state", "zip_code", "surface")} | {
            "crew_notes": data["crew_notes"].strip()
        }


class ContactForm(forms.Form):
    name = forms.CharField(
        max_length=150, widget=forms.TextInput(attrs={"class": INPUT, "autocomplete": "name"})
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"class": INPUT, "autocomplete": "email"})
    )
    phone = forms.CharField(
        max_length=30,
        widget=forms.TextInput(attrs={"class": INPUT, "autocomplete": "tel", "type": "tel"}),
    )
    tax_exempt = forms.BooleanField(
        label="We are a tax-exempt organization",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": CHOICE_INPUT}),
    )
    org_name = forms.CharField(
        label="Organization name",
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={"class": INPUT, "autocomplete": "organization"}),
    )
    cert_number = forms.CharField(
        label="Exemption certificate number",
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={"class": INPUT}),
    )

    def clean_phone(self):
        phone = self.cleaned_data["phone"].strip()
        if len(re.sub(r"\D", "", phone)) < 10:
            raise forms.ValidationError("Enter a phone number with area code.")
        return phone

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("tax_exempt"):
            if not (cleaned.get("org_name") or "").strip():
                self.add_error("org_name", "Enter the organization's name.")
            if not (cleaned.get("cert_number") or "").strip():
                self.add_error("cert_number", "Enter the exemption certificate number.")
        return cleaned

    def to_session(self):
        data = self.cleaned_data
        exempt = data["tax_exempt"]
        return {
            "name": data["name"].strip(),
            "email": data["email"],
            "phone": data["phone"],
            "tax_exempt": exempt,
            "org_name": data["org_name"].strip() if exempt else "",
            "cert_number": data["cert_number"].strip() if exempt else "",
        }


class ReviewForm(forms.Form):
    accept_terms = forms.BooleanField(
        required=True,
        error_messages={"required": "Please read and accept the rental terms to book."},
        widget=forms.CheckboxInput(attrs={"class": CHOICE_INPUT}),
    )
    plan = forms.ChoiceField(
        label="How much to pay now",
        choices=PaymentPlan.choices,
        initial=PaymentPlan.DEPOSIT,
        widget=forms.RadioSelect(attrs={"class": CHOICE_INPUT}),
    )
    choice = forms.ChoiceField(
        label="Payment method",
        choices=PaymentChoice.choices,
        initial=PaymentChoice.STRIPE,
        widget=forms.RadioSelect(attrs={"class": CHOICE_INPUT}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        terms_url = reverse("core:terms")
        self.fields["accept_terms"].label = mark_safe(  # noqa: S308
            f'I have read and accept the <a href="{terms_url}" target="_blank" '
            'class="font-bold text-primary underline">rental terms</a>'
        )
