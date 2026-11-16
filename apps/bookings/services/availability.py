"""Availability engine: the only place stock availability is calculated.

A booking ties up its units from BOOKING_BUFFER_DAYS before the event date to
BOOKING_BUFFER_DAYS after it. A variant is available on a date when, on every
day the new booking would tie units up, stock in service exceeds the units
already tied up by other bookings.
"""

from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.bookings.models import Booking, BookingItem
from apps.catalogue.models import ItemVariant
from apps.inventory.services import out_of_service_by_variant

STOCK_HOLDING_STATUSES = (
    Booking.Status.HELD,
    Booking.Status.DEPOSIT_PAID,
    Booking.Status.PAID,
    Booking.Status.DELIVERED,
)


class Shortage:
    """One variant that cannot be supplied in the requested quantity."""

    def __init__(self, variant, requested, available):
        self.variant = variant
        self.requested = requested
        self.available = available

    def __str__(self):
        return f"{self.variant}: requested {self.requested}, only {self.available} available"


class InsufficientStockError(Exception):
    """Raised when one or more variants are short for the requested date."""

    def __init__(self, event_date, shortages):
        self.event_date = event_date
        self.shortages = shortages
        details = "; ".join(str(shortage) for shortage in shortages)
        super().__init__(f"Not enough stock on {event_date:%b %d, %Y}. {details}.")


def _buffer_days():
    return settings.BOOKING_BUFFER_DAYS


def _holds_stock(now):
    """Filter for booking items whose booking currently ties up stock."""
    return Q(
        booking__status__in=(
            Booking.Status.DEPOSIT_PAID,
            Booking.Status.PAID,
            Booking.Status.DELIVERED,
        )
    ) | Q(booking__status=Booking.Status.HELD, booking__hold_expires_at__gt=now)


def _peak_booked(variant_ids, event_date, exclude_booking_id=None):
    """Return {variant_id: most units tied up on any day a booking on event_date needs}."""
    buffer = _buffer_days()
    rows = BookingItem.objects.filter(
        _holds_stock(timezone.now()),
        variant_id__in=variant_ids,
        booking__event_date__range=(
            event_date - timedelta(days=2 * buffer),
            event_date + timedelta(days=2 * buffer),
        ),
    )
    if exclude_booking_id is not None:
        rows = rows.exclude(booking_id=exclude_booking_id)
    rows = rows.values("variant_id", "booking__event_date").annotate(total=Sum("quantity"))

    booked_by_event_date = defaultdict(dict)
    for row in rows:
        booked_by_event_date[row["variant_id"]][row["booking__event_date"]] = row["total"]

    peaks = {}
    for variant_id, by_date in booked_by_event_date.items():
        peak = 0
        for offset in range(-buffer, buffer + 1):
            day = event_date + timedelta(days=offset)
            in_use = sum(
                total for other, total in by_date.items() if abs((other - day).days) <= buffer
            )
            peak = max(peak, in_use)
        peaks[variant_id] = peak
    return peaks


def _available(variants, event_date, exclude_booking_id=None):
    """Return {variant_id: available units}, clamped at zero, for loaded variants."""
    variant_ids = [variant.pk for variant in variants]
    out_of_service = out_of_service_by_variant(variant_ids)
    booked = _peak_booked(variant_ids, event_date, exclude_booking_id)
    return {
        variant.pk: max(
            0,
            variant.stock_quantity - out_of_service.get(variant.pk, 0) - booked.get(variant.pk, 0),
        )
        for variant in variants
    }


def available_quantity(variant, event_date):
    """Units of a variant that can still be booked for an event on event_date."""
    fresh = ItemVariant.objects.get(pk=variant.pk)
    return _available([fresh], event_date)[fresh.pk]


def lock_and_check(quantities, event_date, exclude_booking=None):
    """Lock the variant rows and raise InsufficientStockError if any is short.

    quantities maps variant id to the units wanted. Must run inside
    transaction.atomic(); the locks are held until that transaction ends, so
    the caller can write its booking knowing no one else can take the stock.
    Rows are locked in primary-key order so overlapping requests cannot deadlock.
    exclude_booking is the booking being edited, whose own items must not count
    against it.
    """
    variants = list(
        ItemVariant.objects.select_for_update(of=("self",))
        .select_related("item")
        .filter(pk__in=quantities.keys())
        .order_by("pk")
    )
    exclude_id = exclude_booking.pk if exclude_booking is not None else None
    available = _available(variants, event_date, exclude_id)
    shortages = [
        Shortage(variant, quantities[variant.pk], available[variant.pk])
        for variant in variants
        if quantities[variant.pk] > available[variant.pk]
    ]
    if shortages:
        raise InsufficientStockError(event_date, shortages)
    return variants


def reserve(booking_items, event_date, *, customer_name, email, phone, delivery_address):
    """Create a held booking for the requested items, or raise if any is short.

    booking_items is an iterable of (variant, quantity) pairs. The hold lapses
    HOLD_MINUTES from now unless the booking moves on to a paid status.
    """
    quantities = defaultdict(int)
    for variant, quantity in booking_items:
        if quantity < 1:
            raise ValueError("Each booking item needs a quantity of at least 1.")
        quantities[variant.pk] += quantity
    if not quantities:
        raise ValueError("A reservation needs at least one item.")

    with transaction.atomic():
        variants = lock_and_check(quantities, event_date)
        if len(variants) != len(quantities):
            raise ItemVariant.DoesNotExist("A requested variant no longer exists.")
        booking = Booking.objects.create(
            customer_name=customer_name,
            email=email,
            phone=phone,
            delivery_address=delivery_address,
            event_date=event_date,
            status=Booking.Status.HELD,
            hold_expires_at=timezone.now() + timedelta(minutes=settings.HOLD_MINUTES),
        )
        BookingItem.objects.bulk_create(
            BookingItem(
                booking=booking,
                variant=variant,
                quantity=quantities[variant.pk],
                unit_price=variant.unit_price,
            )
            for variant in variants
        )
    return booking


def staff_hold_expiry():
    """When a hold placed by staff lapses if they do not choose a time."""
    return timezone.now() + timedelta(hours=settings.ADMIN_HOLD_HOURS)


def release_expired_holds():
    """Mark lapsed holds as expired and return how many were released."""
    now = timezone.now()
    return Booking.objects.filter(status=Booking.Status.HELD, hold_expires_at__lte=now).update(
        status=Booking.Status.EXPIRED, updated_at=now
    )
