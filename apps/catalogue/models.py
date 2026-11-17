"""Catalogue models: categories, rentable items and their variants."""

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.urls import reverse

from apps.core.models import TimeStampedModel

IMAGE_MAX_BYTES = 10 * 1024 * 1024


def validate_image_size(file):
    """Reject uploads over 10 MB; they are downscaled afterwards but must be sane to decode."""
    if file.size > IMAGE_MAX_BYTES:
        raise ValidationError("Images must be 10 MB or smaller.")


class RenditionsMixin(models.Model):
    """Fields filled in by the image task: sizes, formats and a blurred placeholder.

    ``renditions`` is a list of {"format", "width", "height", "name"} dicts, one per
    generated file. ``renditions_ready`` is False until the Celery task has run, and
    templates show a placeholder box until then.
    """

    image_width = models.PositiveIntegerField(null=True, blank=True, editable=False)
    image_height = models.PositiveIntegerField(null=True, blank=True, editable=False)
    placeholder = models.TextField(blank=True, editable=False)
    renditions = models.JSONField(default=list, blank=True, editable=False)
    renditions_ready = models.BooleanField(default=False, editable=False)

    class Meta:
        abstract = True

    @classmethod
    def from_db(cls, db, field_names, values):
        """Remember the loaded file name so a replaced upload can be detected on save."""
        instance = super().from_db(db, field_names, values)
        instance._loaded_image_name = instance.image.name if "image" in field_names else None
        return instance


class Category(RenditionsMixin, TimeStampedModel):
    """A group of rentable items, such as Chairs or Bounce Houses."""

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to="categories/", blank=True, validators=[validate_image_size])
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("catalogue:category", args=[self.slug])


class Item(TimeStampedModel):
    """A rentable product. Stock and availability live on its variants."""

    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="items")
    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=170, unique=True)
    short_description = models.CharField(
        max_length=200, blank=True, help_text="One line shown on catalogue cards."
    )
    description = models.TextField(blank=True)
    whats_included = models.JSONField(
        default=list, blank=True, help_text="List of strings, one per bullet."
    )
    specs = models.JSONField(
        default=list,
        blank=True,
        help_text='Ordered list of {"name": ..., "value": ...} pairs, e.g. Size: 13 x 13 ft.',
    )
    is_featured = models.BooleanField(default=False)
    needs_power = models.BooleanField(
        default=False, help_text="Needs a power outlet at the setup spot (inflatables, machines)."
    )
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

    def get_absolute_url(self):
        return reverse("catalogue:item", args=[self.slug])

    def display_price(self):
        """Lowest positive variant price, or None when no price is set ("Call for price").

        Uses prefetched variants when present. A price of 0 means not yet priced.
        """
        prices = [variant.unit_price for variant in self.variants.all()]
        positive = [price for price in prices if price > 0]
        if positive:
            return min(positive)
        return self.base_price if self.base_price > 0 else None

    def cover_image(self):
        """The primary image, else the first by sort order, from prefetched images; or None."""
        return next(iter(self.images.all()), None)

    def has_price_range(self):
        """True when variants carry different positive prices, so "From $X" applies."""
        prices = {variant.unit_price for variant in self.variants.all() if variant.unit_price > 0}
        return len(prices) > 1


class ItemImage(RenditionsMixin, TimeStampedModel):
    """A photo of an item. The first by sort order is shown when none is primary."""

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="items/", validators=[validate_image_size])
    alt_text = models.CharField(
        max_length=200, help_text="Describe the photo for people using screen readers."
    )
    sort_order = models.PositiveIntegerField(default=0)
    is_primary = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_primary", "sort_order", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["item"],
                condition=models.Q(is_primary=True),
                name="one_primary_image_per_item",
            ),
        ]

    def __str__(self):
        return f"{self.item.name} photo {self.sort_order}"


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

    stock_confirmed = models.BooleanField(
        default=True,
        help_text="Untick while the stock count is a placeholder. The site then says "
        '"Call to confirm availability" instead of showing a count or "Unavailable".',
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

    def get_absolute_url(self):
        return reverse("catalogue:package", args=[self.slug])


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
