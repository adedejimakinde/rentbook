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
