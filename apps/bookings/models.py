"""Booking models: a customer's booking and the variants it reserves."""

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import DecimalField, ExpressionWrapper, F, ProtectedError, Q, Sum
from django.db.models.functions import Lower

from apps.core.models import TimeStampedModel


def normalise_zip(value):
    """Return the 5-digit ZIP from user input ("75001" or "75001-1234"), or "" if invalid."""
    digits = str(value or "").strip().split("-")[0]
    return digits if len(digits) == 5 and digits.isdigit() else ""


class DeliveryZone(TimeStampedModel):
    """A group of ZIP codes served for the same delivery and setup fees."""

    name = models.CharField(max_length=100, unique=True)
    zip_codes = ArrayField(
        models.CharField(max_length=5),
        help_text="5-digit ZIP codes, separated by commas.",
    )
    delivery_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    setup_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        indexes = [GinIndex(fields=["zip_codes"], name="delivery_zone_zip_codes_gin")]

    def __str__(self):
        return self.name

    def clean(self):
        """ZIPs must be valid, and a ZIP belongs to at most one active zone."""
        raw = self.zip_codes or []
        cleaned = [normalise_zip(code) for code in raw]
        invalid = [str(code) for code, zip_code in zip(raw, cleaned, strict=True) if not zip_code]
        if invalid:
            raise ValidationError({"zip_codes": f"Not a 5-digit ZIP code: {', '.join(invalid)}."})
        self.zip_codes = sorted(set(cleaned))
        if not self.is_active:
            return
        clashes = DeliveryZone.objects.filter(
            is_active=True, zip_codes__overlap=self.zip_codes
        ).exclude(pk=self.pk)
        problems = [
            f"{', '.join(sorted(set(zone.zip_codes) & set(self.zip_codes)))} already in {zone.name}"
            for zone in clashes
        ]
        if problems:
            raise ValidationError(
                {"zip_codes": "A ZIP can be in only one active zone: " + "; ".join(problems) + "."}
            )


class PromoCode(TimeStampedModel):
    """A discount code taking a percentage or a fixed amount off items and packages."""

    code = models.CharField(max_length=40, help_text="Not case-sensitive.")
    percent_off = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01")), MaxValueValidator(100)],
    )
    amount_off = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    min_subtotal = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Smallest items and packages subtotal the code applies to.",
    )
    max_uses = models.PositiveIntegerField(
        null=True, blank=True, help_text="Leave empty for unlimited uses."
    )
    uses_count = models.PositiveIntegerField(
        default=0, help_text="Bookings that used this code and made a payment."
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(Lower("code"), name="promo_code_unique_case_insensitive"),
            models.CheckConstraint(
                condition=(
                    Q(percent_off__isnull=False, amount_off__isnull=True)
                    | Q(percent_off__isnull=True, amount_off__isnull=False)
                ),
                name="promo_percent_or_amount_not_both",
            ),
            models.CheckConstraint(
                condition=Q(percent_off__isnull=True)
                | (Q(percent_off__gt=0) & Q(percent_off__lte=100)),
                name="promo_percent_in_range",
            ),
            models.CheckConstraint(
                condition=Q(amount_off__isnull=True) | Q(amount_off__gt=0),
                name="promo_amount_positive",
            ),
        ]

    def __str__(self):
        return self.code

    def clean(self):
        """Store the code upper-case and require exactly one kind of discount."""
        self.code = (self.code or "").strip().upper()
        if (self.percent_off is None) == (self.amount_off is None):
            raise ValidationError("Set either a percent off or an amount off, not both.")
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValidationError({"valid_until": "Must be after the start of validity."})

    @property
    def is_used_up(self):
        """True when the code has reached its maximum number of uses."""
        return self.max_uses is not None and self.uses_count >= self.max_uses


class BookingDeleteRefused(ProtectedError):
    """Raised when something tries to delete a booking that holds stock or has payments.

    A ProtectedError, so callers that already handle protected deletes (the
    admin among them) show it as a refusal rather than a server error.
    """

    def __init__(self, message, booking):
        super().__init__(message, [booking])

    def __str__(self):
        return self.args[0]


def check_deletable(booking):
    """Raise BookingDeleteRefused if the booking holds stock or has any payment entry."""
    from apps.bookings.services import lifecycle

    if lifecycle.holds_stock(booking):
        raise BookingDeleteRefused(
            f"{booking} holds stock and cannot be deleted. Cancel it.", booking
        )
    if booking.payments.exists():
        raise BookingDeleteRefused(f"{booking} has payments on it and cannot be deleted.", booking)


class BookingQuerySet(models.QuerySet):
    def delete(self):
        """Refuse the whole delete, before anything is removed, if any booking is live."""
        for booking in self:
            check_deletable(booking)
        return super().delete()


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
    zip_code = models.CharField(
        max_length=5, blank=True, help_text="Delivery ZIP the fees were quoted for."
    )
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
    setup_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    discount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Taken off items and packages only.",
    )
    tax = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
        help_text=(
            "Sales tax on the lines after discount. Recalculated whenever staff save the "
            "booking, until it has a payment."
        ),
    )
    caution_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
        help_text=(
            "Refundable caution deposit, collected on top of the rental total. "
            "Recalculated from the lines whenever staff save the booking, until it has a payment."
        ),
    )
    promo_code = models.ForeignKey(
        PromoCode, on_delete=models.PROTECT, null=True, blank=True, related_name="bookings"
    )
    promo_redeemed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the booking's first payment was checked against the promo code.",
    )
    needs_attention = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Set when a payment or refund leaves the booking needing staff follow-up.",
    )

    delivery_override_note = models.TextField(
        blank=True,
        help_text="Why the booking was delivered with money or a payment outstanding.",
    )
    delivery_override_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="delivery_overrides",
    )
    delivery_override_at = models.DateTimeField(null=True, blank=True)

    clearing_override_note = models.TextField(
        blank=True,
        help_text="Cancellations and line changes made while an ACH payment was clearing.",
    )
    clearing_override_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="clearing_overrides",
    )
    clearing_override_at = models.DateTimeField(null=True, blank=True)

    tax_exempt = models.BooleanField(
        default=False, help_text="No sales tax is charged (for example a church or school)."
    )
    tax_exempt_certificate = models.TextField(
        blank=True, help_text="Exemption certificate number and issuer. Required when exempt."
    )
    tax_exempt_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="tax_exemptions_set",
    )
    tax_exempt_at = models.DateTimeField(null=True, blank=True)

    objects = BookingQuerySet.as_manager()

    class Meta:
        ordering = ["-event_date", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(status="held") | models.Q(hold_expires_at__isnull=False),
                name="held_booking_has_expiry",
            ),
            models.CheckConstraint(
                condition=Q(tax_exempt=False) | ~Q(tax_exempt_certificate=""),
                name="tax_exempt_booking_has_certificate",
            ),
        ]
        permissions = [("can_set_tax_exemption", "Can set tax exemption")]

    def __str__(self):
        return f"Booking #{self.pk} for {self.customer_name} on {self.event_date}"

    def delete(self, *args, **kwargs):
        """Refuse to delete a booking that holds stock or has payments."""
        check_deletable(self)
        return super().delete(*args, **kwargs)

    @property
    def total(self):
        """Rental total: items and packages less discount, plus delivery, setup and tax.

        Excludes the caution fee, which the ledger tracks separately.
        """
        money = DecimalField(max_digits=10, decimal_places=2)
        item_line = ExpressionWrapper(
            (F("quantity") - F("package_quantity")) * F("unit_price"), output_field=money
        )
        package_line = ExpressionWrapper(F("quantity") * F("unit_price"), output_field=money)
        items = self.items.aggregate(value=Sum(item_line))["value"] or Decimal("0.00")
        packages = self.packages.aggregate(value=Sum(package_line))["value"] or Decimal("0.00")
        total = items + packages - self.discount + self.delivery_fee + self.setup_fee + self.tax
        return max(total, Decimal("0.00")).quantize(Decimal("0.01"))

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

    @property
    def overpaid(self):
        """Rental money received beyond the total: refundable to the customer."""
        return max(self.amount_paid - self.total, Decimal("0.00"))

    @property
    def caution_due(self):
        """Caution owed for the current lines and not yet collected."""
        return max(self.caution_fee - self.payments.caution_collected(), Decimal("0.00"))

    @property
    def caution_excess(self):
        """Caution held beyond what the current lines need: to refund to the customer."""
        return max(self.caution_held - self.caution_fee, Decimal("0.00"))


class BookingItem(TimeStampedModel):
    """A quantity of one variant reserved on a booking, at the price agreed when added.

    quantity is every unit reserved. package_quantity of those units belong to
    the booking's packages and are paid for in the package price, so only the
    rest are charged at unit_price.
    """

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="items")
    variant = models.ForeignKey(
        "catalogue.ItemVariant", on_delete=models.PROTECT, related_name="booking_items"
    )
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_price = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(0)]
    )
    package_quantity = models.PositiveIntegerField(
        default=0, help_text="Units reserved for packages; not charged per unit."
    )

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1), name="booking_item_quantity_at_least_1"
            ),
            models.CheckConstraint(
                condition=models.Q(package_quantity__lte=F("quantity")),
                name="booking_item_package_quantity_within_quantity",
            ),
            models.UniqueConstraint(
                fields=["booking", "variant"], name="unique_variant_per_booking"
            ),
        ]

    def __str__(self):
        return f"{self.quantity} x {self.variant}"


class BookingPackage(TimeStampedModel):
    """A package on a booking, at the price agreed when it was added.

    The stock it reserves lives on the booking's items (package_quantity).
    """

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="packages")
    package = models.ForeignKey(
        "catalogue.Package", on_delete=models.PROTECT, related_name="booking_packages"
    )
    name = models.CharField(max_length=150)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_price = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(0)]
    )

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1), name="booking_package_quantity_at_least_1"
            ),
            models.UniqueConstraint(
                fields=["booking", "package"], name="unique_package_per_booking"
            ),
        ]

    def __str__(self):
        return f"{self.quantity} x {self.name}"
