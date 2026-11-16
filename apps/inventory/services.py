"""Inventory: units out of service, and the stock changes that can leave bookings short."""

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.catalogue.models import ItemVariant
from apps.inventory.models import StockAdjustment


def out_of_service_by_variant(variant_ids):
    """Return {variant_id: units damaged or under repair} for the given variants."""
    rows = (
        StockAdjustment.objects.filter(variant_id__in=variant_ids, resolved_at__isnull=True)
        .values("variant_id")
        .annotate(total=Sum("quantity"))
    )
    return {row["variant_id"]: row["total"] for row in rows}


def out_of_service_quantity(variant, exclude=None):
    """Units of one variant that are damaged or under repair, optionally ignoring one row."""
    adjustments = StockAdjustment.objects.filter(variant=variant, resolved_at__isnull=True)
    if exclude is not None and exclude.pk:
        adjustments = adjustments.exclude(pk=exclude.pk)
    return adjustments.aggregate(total=Sum("quantity"))["total"] or 0


def _lock_variants(variant_ids):
    """Lock variant rows in primary-key order, as the availability engine does."""
    return list(ItemVariant.objects.select_for_update().filter(pk__in=variant_ids).order_by("pk"))


def _flag_after_commit(bookings):
    """Flag bookings for staff once the stock change has committed.

    Done after commit so no booking row is locked while variant rows are: the
    ledger always locks a booking before its stock.
    """
    from apps.bookings.models import Booking

    ids = [booking.pk for booking in bookings]
    if ids:
        transaction.on_commit(
            lambda: Booking.objects.filter(pk__in=ids).update(
                needs_attention=True, updated_at=timezone.now()
            )
        )


def _left_short(variant_ids):
    from apps.bookings.services import availability

    short = availability.bookings_left_short(variant_ids)
    _flag_after_commit(short)
    return short


def save_adjustment(adjustment):
    """Save a stock adjustment and return the bookings it leaves short.

    The variant row is locked first and the adjustment validated again under
    the lock, so two adjustments at once cannot take out more units than
    exist. The change is saved even when bookings are left short: damage is a
    fact, so those bookings are flagged for staff instead.
    """
    with transaction.atomic():
        _lock_variants([adjustment.variant_id])
        adjustment.full_clean()
        adjustment.save()
        return _left_short([adjustment.variant_id])


def save_variants(variants):
    """Save changed variants (their stock) and return the bookings left short.

    Locks the rows and validates each again under the lock before saving.
    """
    variants = list(variants)
    with transaction.atomic():
        _lock_variants([variant.pk for variant in variants if variant.pk])
        for variant in variants:
            variant.full_clean()
            variant.save()
        return _left_short([variant.pk for variant in variants])


def shortage_warning(bookings):
    """Admin message naming the bookings a stock change left short, or "" if none."""
    if not bookings:
        return ""
    names = ", ".join(
        f"#{booking.pk} ({booking.customer_name}, {booking.event_date:%b %d, %Y})"
        for booking in bookings
    )
    return (
        f"Saved, but these bookings are now short of stock and have been flagged: {names}. "
        "Arrange replacement units or contact the customers."
    )
