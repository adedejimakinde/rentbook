"""Catalogue models: categories, rentable items and their variants."""

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from apps.core.models import TimeStampedModel


class Category(TimeStampedModel):
    """A group of rentable items, such as Chairs or Bounce Houses."""

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Item(TimeStampedModel):
    """A rentable product. Stock and availability live on its variants."""

    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="items")
    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=170, unique=True)
    description = models.TextField(blank=True)
    base_price = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(0)]
    )
    caution_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Refundable caution deposit per unit rented. Leave at 0 for none.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ItemVariant(TimeStampedModel):
    """A stock-keeping version of an item, such as "Black" or "Default"."""

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="variants")
    name = models.CharField(max_length=100, default="Default")
    price_override = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text="Leave empty to use the item's base price.",
    )
    stock_quantity = models.PositiveIntegerField(
        default=0, help_text="Total units owned, including damaged and under-repair units."
    )

    class Meta:
        ordering = ["item__name", "name"]
        constraints = [
            models.UniqueConstraint(fields=["item", "name"], name="unique_variant_name_per_item"),
        ]

    def __str__(self):
        return f"{self.item.name} ({self.name})"

    @property
    def unit_price(self):
        """Price per unit: the override if set, otherwise the item's base price."""
        if self.price_override is not None:
            return self.price_override
        return self.item.base_price

    def clean(self):
        """Stock cannot drop below the units currently out of service."""
        from apps.inventory.services import out_of_service_quantity

        if self.pk and self.stock_quantity is not None:
            out_of_service = out_of_service_quantity(self)
            if self.stock_quantity < out_of_service:
                raise ValidationError(
                    {
                        "stock_quantity": (
                            f"{out_of_service} unit(s) are damaged or under repair; "
                            "stock cannot be lower than that."
                        )
                    }
                )


class Package(TimeStampedModel):
    """A fixed bundle of variants rented together for one price."""

    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=170, unique=True)
    package_price = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(0)]
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class PackageItem(TimeStampedModel):
    """A quantity of one variant included in each unit of a package."""

    package = models.ForeignKey(Package, on_delete=models.CASCADE, related_name="items")
    variant = models.ForeignKey(ItemVariant, on_delete=models.PROTECT, related_name="package_items")
    quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1), name="package_item_quantity_at_least_1"
            ),
            models.UniqueConstraint(
                fields=["package", "variant"], name="unique_variant_per_package"
            ),
        ]

    def __str__(self):
        return f"{self.quantity} x {self.variant}"
