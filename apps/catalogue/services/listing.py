"""Catalogue queries and the availability wording shown to visitors."""

from dataclasses import dataclass
from urllib.parse import quote

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Count, Prefetch, Q

from apps.bookings.services import availability
from apps.catalogue.models import Category, Item, ItemImage

PAGE_SIZE = 12
LOW_STOCK = 3

AVAILABLE = "available"
LIMITED = "limited"
UNAVAILABLE = "unavailable"
CONFIRM = "confirm"


@dataclass(frozen=True)
class Status:
    """What to tell a visitor about stock on their date."""

    code: str
    label: str
    quantity: int = 0


CALL_TO_CONFIRM = Status(CONFIRM, "Call to confirm availability")


def active_items():
    """Active items with everything a card needs loaded in three queries."""
    return (
        Item.objects.filter(is_active=True)
        .select_related("category")
        .prefetch_related(
            "variants",
            Prefetch(
                "images", queryset=ItemImage.objects.order_by("-is_primary", "sort_order", "pk")
            ),
        )
    )


def categories_with_items():
    """Categories that have at least one active item, with the count."""
    return (
        Category.objects.annotate(item_count=Count("items", filter=Q(items__is_active=True)))
        .filter(item_count__gt=0)
        .order_by("sort_order", "name")
    )


def catalogue_page(category, page_number):
    """One page of the catalogue, optionally limited to a category."""
    items = active_items().order_by("category__sort_order", "category__name", "name")
    if category is not None:
        items = items.filter(category=category)
    return Paginator(items, PAGE_SIZE).get_page(page_number)


def variant_status(variant, in_stock, wanted=1):
    """Status of one variant, given {variant_id: bookable units} from the availability engine."""
    if not variant.stock_confirmed:
        return CALL_TO_CONFIRM
    units = in_stock.get(variant.pk, 0)
    if units < wanted:
        if units == 0:
            return Status(UNAVAILABLE, "Unavailable for this date")
        return Status(LIMITED, f"Only {units} available for this date", units)
    if units <= LOW_STOCK:
        return Status(LIMITED, f"Only {units} left for this date", units)
    return Status(AVAILABLE, "Available for this date", units)


def item_statuses(items, event_date):
    """Return {item_id: Status} for a page of items on one event date.

    Runs the availability engine once for every variant on the page. An item counts
    as available when any confirmed variant has stock; if none does but a variant's
    stock is unconfirmed, the visitor is told to call.
    """
    variants = [variant for item in items for variant in item.variants.all()]
    in_stock = availability.available_quantities(variants, event_date) if variants else {}
    statuses = {}
    for item in items:
        per_variant = [variant_status(variant, in_stock) for variant in item.variants.all()]
        bookable = [s for s in per_variant if s.code in (AVAILABLE, LIMITED)]
        if bookable:
            statuses[item.pk] = max(bookable, key=lambda s: s.quantity)
        elif any(s.code == CONFIRM for s in per_variant) or not per_variant:
            statuses[item.pk] = CALL_TO_CONFIRM
        else:
            statuses[item.pk] = Status(UNAVAILABLE, "Unavailable for this date")
    return statuses


def featured_items(limit=8):
    """Items flagged as featured, for the home page row."""
    return list(active_items().filter(is_featured=True).order_by("name")[:limit])


def related_items(item, limit=4):
    """Other active items from the same category."""
    return list(
        active_items().filter(category=item.category).exclude(pk=item.pk).order_by("name")[:limit]
    )


DEFAULT_VARIANT_NAME = "Default"


def price_enquiry_url(name, variant_name="", event_date=None):
    """A WhatsApp link asking for a price, prefilled with the rental, option and event date.

    For rentals with no online price ("Call for price"), which cannot go in the cart.
    """
    rental = name
    if variant_name and variant_name != DEFAULT_VARIANT_NAME:
        rental = f"{name} ({variant_name})"
    message = f"Hi, could I get a price for the {rental}"
    if event_date:
        message += f" for my event on {event_date:%A, %B} {event_date.day}, {event_date.year}"
    message += "?"
    return f"https://wa.me/{settings.WHATSAPP_NUMBER}?text={quote(message)}"
