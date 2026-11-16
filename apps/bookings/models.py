"""Booking models: a customer's booking and the variants it reserves."""

from django.core.validators import MinValueValidator
from django.db import models

from apps.core.models import TimeStampedModel


class Booking(TimeStampedModel):
    """A customer's request for items on one event date."""

    class Status(models.TextChoices):
        ENQUIRY = "enquiry", "Enquiry"
        QUOTED = "quoted", "Quoted"
        HELD = "held", "Held"
        DEPOSIT_PAID = "deposit_paid", "Deposit paid"
        PAID = "paid", "Paid"
        DELIVERED = "delivered", "Delivered"
        RETURNED = "returned", "Returned"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    customer_name = models.CharField(max_length=150)
    email = models.EmailField()
    phone = models.CharField(max_length=30)
    event_date = models.DateField(db_index=True)
    delivery_address = models.TextField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ENQUIRY, db_index=True
    )
    hold_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When a held booking lapses. Empty on a held booking means it never lapses.",
    )

    class Meta:
        ordering = ["-event_date", "-created_at"]

    def __str__(self):
        return f"Booking #{self.pk} for {self.customer_name} on {self.event_date}"


class BookingItem(TimeStampedModel):
    """A quantity of one variant on a booking, at the price agreed when it was added."""

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="items")
    variant = models.ForeignKey(
        "catalogue.ItemVariant", on_delete=models.PROTECT, related_name="booking_items"
    )
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_price = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(0)]
    )

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1), name="booking_item_quantity_at_least_1"
            ),
            models.UniqueConstraint(
                fields=["booking", "variant"], name="unique_variant_per_booking"
            ),
        ]

    def __str__(self):
        return f"{self.quantity} x {self.variant}"
