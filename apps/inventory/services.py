"""Inventory queries shared with the availability engine."""

from django.db.models import Sum

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
