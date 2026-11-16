"""Payment models: every movement of money against a booking, and seen Stripe events."""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import F, Q

from apps.core.models import TimeStampedModel

ZERO = Decimal("0.00")


class PaymentQuerySet(models.QuerySet):
    """Money totals over a set of payments (normally one booking's)."""

    def _received(self):
        return self.filter(
            kind__in=Payment.RENTAL_KINDS,
            status__in=(Payment.Status.SUCCEEDED, Payment.Status.REFUNDED),
        )

    def rental_paid(self):
        """Rental money received, net of refunds. Excludes caution money."""
        return sum((payment.rental_net for payment in self._received()), ZERO)

    def caution_collected(self):
        """Caution money received, net of any refunded through Stripe."""
        return sum((payment.caution_net for payment in self._received()), ZERO)

    def caution_held(self):
        """Caution money collected and not yet refunded or kept for damage."""
        released = self.filter(
            kind__in=Payment.CAUTION_RELEASE_KINDS, status=Payment.Status.SUCCEEDED
        )
        return self.caution_collected() - sum((payment.amount for payment in released), ZERO)


class Payment(TimeStampedModel):
    """One movement of money against a booking.

    amount is the rental portion. caution_amount is caution deposit money taken
    in the same payment; it never counts towards the booking's amount paid.
    A rental refund entry is the record of money staff sent back by hand; the
    refunded money itself is counted in refunded_amount on the payment it
    refunds, so these entries never enter any total.
    """

    class Method(models.TextChoices):
        CARD = "card", "Card"
        ACH = "ach", "ACH bank transfer"
        ZELLE = "zelle", "Zelle"
        CASHAPP = "cashapp", "Cash App"
        BANK_DEPOSIT = "bank_deposit", "Bank deposit"

    class Kind(models.TextChoices):
        DEPOSIT = "deposit", "Deposit"
        FULL = "full", "Full payment"
        BALANCE = "balance", "Balance"
        CAUTION_REFUND = "caution_refund", "Caution refund"
        DAMAGE_CHARGE = "damage_charge", "Damage charge"
        RENTAL_REFUND = "rental_refund", "Rental refund"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"

    RENTAL_KINDS = (Kind.DEPOSIT, Kind.FULL, Kind.BALANCE)
    CAUTION_RELEASE_KINDS = (Kind.CAUTION_REFUND, Kind.DAMAGE_CHARGE)
    MANUAL_METHODS = (Method.ZELLE, Method.CASHAPP, Method.BANK_DEPOSIT)

    booking = models.ForeignKey(
        "bookings.Booking", on_delete=models.PROTECT, related_name="payments"
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    caution_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    refunded_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="Refunded so far. Counts against the rental amount first, then caution.",
    )
    method = models.CharField(max_length=20, choices=Method.choices, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    stripe_checkout_session_id = models.CharField(  # noqa: DJ001
        max_length=255, unique=True, null=True, blank=True
    )
    stripe_payment_intent_id = models.CharField(  # noqa: DJ001
        max_length=255, unique=True, null=True, blank=True
    )
    stripe_refund_id = models.CharField(  # noqa: DJ001
        max_length=255,
        unique=True,
        null=True,
        blank=True,
        help_text="Set on a caution refund sent back through Stripe.",
    )
    refund_of = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="refunds",
        help_text="The payment a refund entry was sent back against.",
    )
    reference = models.CharField(max_length=255, blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payments_received",
    )
    received_at = models.DateTimeField(null=True, blank=True)

    objects = PaymentQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gte=0)
                & Q(caution_amount__gte=0)
                & Q(amount__gt=-F("caution_amount")),
                name="payment_amounts_positive",
            ),
            models.CheckConstraint(
                condition=Q(refunded_amount__gte=0)
                & Q(refunded_amount__lte=F("amount") + F("caution_amount")),
                name="payment_refund_within_amount",
            ),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} of ${self.charged_total} on booking #{self.booking_id}"

    @property
    def charged_total(self):
        """Everything this payment moved: rental plus caution."""
        return self.amount + self.caution_amount

    @property
    def rental_net(self):
        """Rental money this payment still contributes after refunds."""
        return self.amount - min(self.refunded_amount, self.amount)

    @property
    def caution_net(self):
        """Caution money this payment still contributes after refunds."""
        return self.caution_amount - max(self.refunded_amount - self.amount, ZERO)

    @property
    def caution_refundable(self):
        """Caution money that can still be sent back against this Stripe payment."""
        if not self.stripe_payment_intent_id:
            return ZERO
        caution_refunds = self.refunds.filter(kind=Payment.Kind.CAUTION_REFUND)
        sent_back = sum((refund.amount for refund in caution_refunds), ZERO)
        return max(self.caution_net - sent_back, ZERO)


class StripeEvent(TimeStampedModel):
    """A Stripe webhook event that has been processed. Makes handling idempotent."""

    event_id = models.CharField(max_length=255, unique=True)
    type = models.CharField(max_length=100)

    def __str__(self):
        return f"{self.type} ({self.event_id})"
