"""Booking models: a customer's booking and the variants it reserves."""

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import DecimalField, ExpressionWrapper, F, Sum

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
        help_text="When a held booking lapses and releases its stock.",
    )
    delivery_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    caution_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Refundable caution deposit, collected on top of the rental total.",
    )
    needs_attention = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Set when a payment or refund leaves the booking needing staff follow-up.",
    )

    class Meta:
        ordering = ["-event_date", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(status="held") | models.Q(hold_expires_at__isnull=False),
                name="held_booking_has_expiry",
            ),
        ]

    def __str__(self):
        return f"Booking #{self.pk} for {self.customer_name} on {self.event_date}"

    @property
    def total(self):
        """Rental total: item lines plus delivery. Excludes the caution fee."""
        line = ExpressionWrapper(
            F("quantity") * F("unit_price"),
            output_field=DecimalField(max_digits=10, decimal_places=2),
        )
        lines = self.items.aggregate(value=Sum(line))["value"] or Decimal("0.00")
        return (lines + self.delivery_fee).quantize(Decimal("0.01"))

    @property
    def deposit_amount(self):
        """The share of the total (DEPOSIT_PERCENT) that secures the booking."""
        share = self.total * Decimal(settings.DEPOSIT_PERCENT) / Decimal(100)
        return share.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def amount_paid(self):
        """Rental money received and not refunded. Caution money is never included."""
        return self.payments.rental_paid()

    @property
    def balance_due(self):
        """What is still owed on the rental total, never below zero."""
        return max(self.total - self.amount_paid, Decimal("0.00"))

    @property
    def caution_held(self):
        """Caution money collected and not yet refunded or kept for damage."""
        return self.payments.caution_held()


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
