"""Catalogue queries and the availability wording shown to visitors."""

from dataclasses import dataclass
from urllib.parse import quote

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Case, Count, DecimalField, F, Min, Prefetch, Q, When
from django.db.models.functions import Coalesce

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

    @property
    def short_label(self):
        """The label without its tail, for cards on small screens: "Available"."""
        if self.code == CONFIRM:
            return "Call to confirm"
        return self.label.removesuffix(" for this date")


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


POPULAR = "popular"
PRICE_LOW = "price-low"
PRICE_HIGH = "price-high"
NAME = "name"
SORTS = {
    POPULAR: "Popular",
    PRICE_LOW: "Price: low to high",
    PRICE_HIGH: "Price: high to low",
    NAME: "Name: A to Z",
}


def clean_sort(value):
    """A known sort key, or the default (popular first)."""
    return value if value in SORTS else POPULAR


def _with_sort_price(items):
    """Annotate each item with the price the card shows (see Item.display_price).

    That is the lowest positive variant price, else the item's base price when positive,
    else NULL ("Call for price"), so priceless items can be sorted last.
    """
    money = DecimalField(max_digits=10, decimal_places=2)
    unit = Coalesce("variants__price_override", "base_price", output_field=money)
    lowest = Min(
        Case(
            When(
                Q(variants__price_override__gt=0)
                | Q(variants__price_override__isnull=True, base_price__gt=0),
                then=unit,
            ),
            output_field=money,
        )
    )
    fallback = Case(When(base_price__gt=0, then=F("base_price")), output_field=money)
    return items.annotate(sort_price=Coalesce(lowest, fallback, output_field=money))


def catalogue_page(category, page_number, sort=POPULAR):
    """One page of the catalogue, optionally limited to a category, in the chosen order."""
    items = active_items()
    if category is not None:
        items = items.filter(category=category)
    group = ("category__sort_order", "category__name", "name")
    if sort == NAME:
        items = items.order_by("name", "pk")
    elif sort in (PRICE_LOW, PRICE_HIGH):
        price = F("sort_price")
        price = price.asc(nulls_last=True) if sort == PRICE_LOW else price.desc(nulls_last=True)
        items = _with_sort_price(items).order_by(price, "name", "pk")
    else:
        items = items.order_by("-is_featured", *group, "pk")
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


def line_total(variant, quantity):
    """What `quantity` of a variant costs for the event, as the quote prices it, or None."""
    from apps.bookings.services.quote import to_cents

    if variant is None or not variant.unit_price:
        return None
    return to_cents(variant.unit_price * quantity)


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
