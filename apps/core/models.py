"""Shared abstract models."""

from django.conf import settings as django_settings
from django.db import models


class TimeStampedModel(models.Model):
    """Abstract base that records when a row was created and last changed."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SiteSettings(TimeStampedModel):
    """Owner-controlled switches. One row (pk=1); read it through apps.core.site_settings."""

    online_booking_enabled = models.BooleanField(
        "Online booking", default=True, help_text="Off: the cart and booking flow are paused."
    )
    card_ach_enabled = models.BooleanField("Card and ACH (Stripe)", default=True)
    zelle_enabled = models.BooleanField("Zelle", default=True)
    cashapp_enabled = models.BooleanField("Cash App", default=True)
    bank_deposit_enabled = models.BooleanField("Bank deposit", default=True)
    allow_deposit = models.BooleanField(
        "Allow deposits", default=True, help_text="Off: new online bookings pay in full."
    )
    promo_codes_enabled = models.BooleanField("Promo codes", default=True)
    tax_exempt_requests_enabled = models.BooleanField("Tax-exempt requests", default=True)
    sms_enabled = models.BooleanField("SMS", default=True)
    whatsapp_enabled = models.BooleanField("WhatsApp", default=True)
    owner_alerts_enabled = models.BooleanField(
        "Owner text alerts", default=True, help_text="SMS and WhatsApp alerts to OWNER_PHONE."
    )
    reminder_payment_deadline = models.BooleanField("Manual payment deadline", default=True)
    reminder_balance_due = models.BooleanField("Balance due (first)", default=True)
    reminder_balance_final = models.BooleanField("Balance due (2 days before)", default=True)
    reminder_owner_unpaid = models.BooleanField(
        "Owner alert: balance unpaid 1 day before", default=True
    )
    reminder_pre_event = models.BooleanField("Day-before event reminder", default=True)
    reminder_thank_you = models.BooleanField("Thank-you after the event", default=True)

    class Meta:
        verbose_name = "Site settings"
        verbose_name_plural = "Site settings"

    def __str__(self):
        return "Site settings"


class SiteSettingsChange(TimeStampedModel):
    """Who changed which site settings, and when."""

    settings = models.ForeignKey(SiteSettings, on_delete=models.CASCADE, related_name="changes")
    changed_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    changes = models.JSONField(help_text="Field name to [old, new].")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Change on {self.created_at:%Y-%m-%d %H:%M}"
