"""Inventory: units out of service, and the stock changes that can leave bookings short."""

from django.core.exceptions import ValidationError
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


def check_variants_under_lock(changed, deleted=(), exclude=None):
    """Lock the variants being changed or deleted and validate them again under the locks.

    Run from the admin's form validation, inside the request's transaction,
    so the locks are held until the save and a problem shows as a form error.
    Raises ValidationError naming each variant that is no longer valid, and
    each variant to be deleted that a booking now uses. exclude names fields
    not to validate (the item, on a form for an item not saved yet).
    """
    from apps.bookings.models import BookingItem

    changed, deleted = list(changed), list(deleted)
    _lock_variants([variant.pk for variant in changed + deleted if variant.pk])
    problems = []
    for variant in changed:
        try:
            variant.full_clean(exclude=exclude)
        except ValidationError as error:
            problems += [f"{variant}: {message}" for message in error.messages]
    booked = set(
        BookingItem.objects.filter(variant__in=[v for v in deleted if v.pk]).values_list(
            "variant_id", flat=True
        )
    )
    problems += [
        f"{variant} is on bookings and cannot be deleted."
        for variant in deleted
        if variant.pk in booked
    ]
    if problems:
        raise ValidationError(problems)


def save_variants(variants, deleted=()):
    """Save changed variants (their stock), delete removed ones, and return bookings left short.

    Locks every row first and validates each again under the lock before
    saving or deleting.
    """
    variants, deleted = list(variants), list(deleted)
    with transaction.atomic():
        check_variants_under_lock(variants, deleted)
        for variant in deleted:
            variant.delete()
        for variant in variants:
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
