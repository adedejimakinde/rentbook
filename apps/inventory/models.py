"""Inventory models: units taken out of service."""

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import TimeStampedModel


class StockAdjustment(TimeStampedModel):
    """Units of a variant that are out of service until the adjustment is resolved."""

    class Reason(models.TextChoices):
        DAMAGED = "damaged", "Damaged"
        UNDER_REPAIR = "under_repair", "Under repair"

    variant = models.ForeignKey(
        "catalogue.ItemVariant", on_delete=models.CASCADE, related_name="stock_adjustments"
    )
    reason = models.CharField(max_length=20, choices=Reason.choices)
    quantity = models.PositiveIntegerField()
    note = models.TextField(blank=True)
    resolved_at = models.DateTimeField(
        null=True, blank=True, help_text="Set when the units are back in service."
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.quantity} x {self.variant} {self.get_reason_display().lower()}"

    @property
    def is_resolved(self):
        """True once the units have returned to service."""
        return self.resolved_at is not None

    def clean(self):
        """A variant's unresolved adjustments cannot exceed its stock."""
        from apps.inventory.services import out_of_service_quantity

        if self.is_resolved or self.variant_id is None or self.quantity is None:
            return
        others = out_of_service_quantity(self.variant, exclude=self)
        stock = self.variant.stock_quantity
        if others + self.quantity > stock:
            raise ValidationError(
                {
                    "quantity": (
                        f"Only {stock - others} of {stock} unit(s) of {self.variant} "
                        "are left to take out of service."
                    )
                }
            )
