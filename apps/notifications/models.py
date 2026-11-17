"""A record of every email the app sends, so a retried job never sends one twice."""

from django.db import models

from apps.core.models import TimeStampedModel


class SentEmail(TimeStampedModel):
    """One email, keyed by the event it is for (for example "receipt:12")."""

    key = models.CharField(max_length=120, unique=True)
    kind = models.CharField(max_length=40)
    booking = models.ForeignKey(
        "bookings.Booking",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="emails",
    )
    to = models.CharField(max_length=254)
    subject = models.CharField(max_length=255)
    sent_at = models.DateTimeField()

    class Meta:
        ordering = ["-sent_at"]

    def __str__(self):
        return f"{self.kind} to {self.to}"


class Notification(TimeStampedModel):
    """One message on one channel, keyed so it is never sent twice. Staff see these."""

    class Channel(models.TextChoices):
        EMAIL = "email", "Email"
        SMS = "sms", "SMS"
        WHATSAPP = "whatsapp", "WhatsApp"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SCHEDULED = "scheduled", "Waiting for 8 am"
        SENT = "sent", "Sent"
        SKIPPED = "skipped", "Skipped"
        FAILED = "failed", "Failed"

    booking = models.ForeignKey(
        "bookings.Booking",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
    )
    channel = models.CharField(max_length=10, choices=Channel.choices)
    kind = models.CharField(max_length=40)
    unique_key = models.CharField(max_length=200, unique=True)
    recipient = models.CharField(max_length=254, blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    body = models.TextField(blank=True, help_text="The SMS text, or a summary for WhatsApp.")
    variables = models.JSONField(default=dict, blank=True, help_text="WhatsApp template values.")
    send_after = models.DateTimeField(null=True, blank=True, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    provider_id = models.CharField(max_length=64, blank=True)
    error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_channel_display()} {self.kind} ({self.status})"


class SmsOptOut(TimeStampedModel):
    """A phone number that replied STOP. While opted_out is true it gets no SMS or WhatsApp."""

    phone = models.CharField(
        max_length=20, unique=True, help_text="E.164, for example +14695550100."
    )
    opted_out = models.BooleanField(default=True)
    opted_out_at = models.DateTimeField(null=True, blank=True)
    opted_in_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "SMS opt-out"

    def __str__(self):
        return f"{self.phone} ({'opted out' if self.opted_out else 'opted in'})"
