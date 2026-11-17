"""Forms for public site pages."""

from django import forms

INPUT = "min-h-12 w-full rounded-lg border-2 border-primary/40 bg-white px-3 py-2"


class ContactForm(forms.Form):
    """Enquiry form. The hidden ``website`` field is a honeypot that real visitors leave empty."""

    name = forms.CharField(
        max_length=100, widget=forms.TextInput(attrs={"class": INPUT, "autocomplete": "name"})
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"class": INPUT, "autocomplete": "email"})
    )
    phone = forms.CharField(
        max_length=30,
        required=False,
        widget=forms.TextInput(attrs={"class": INPUT, "autocomplete": "tel", "type": "tel"}),
    )
    event_date = forms.DateField(
        required=False,
        label="Event date (optional)",
        widget=forms.DateInput(attrs={"class": INPUT, "type": "date"}),
    )
    message = forms.CharField(
        max_length=3000,
        label="What do you need?",
        widget=forms.Textarea(attrs={"class": INPUT, "rows": 5}),
    )
    website = forms.CharField(
        required=False, widget=forms.TextInput(attrs={"tabindex": "-1", "autocomplete": "off"})
    )
