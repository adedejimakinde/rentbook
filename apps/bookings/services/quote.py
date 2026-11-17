"""Quote builder: prices a cart and turns an accepted quote into a held booking.

Money is Decimal throughout. Each line is rounded once (quantity x unit price)
and each total is rounded once from unrounded inputs, so cents never drift.
Stock is only read here; availability itself is calculated in availability.py.
"""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.bookings.models import (
    BookingItem,
    BookingPackage,
    DeliveryZone,
    PromoCode,
    normalise_zip,
)
from apps.bookings.services import availability
from apps.catalogue.models import ItemVariant, Package

ZERO = Decimal("0.00")
CENT = Decimal("0.01")
HUNDRED = Decimal(100)

ITEM = "item"
PACKAGE = "package"

ZIP_NOT_SERVED = "zip_not_served"
PROMO_INVALID = "promo_invalid"
LINE_UNAVAILABLE = "line_unavailable"
EMPTY_CART = "empty_cart"
EVENT_TOO_SOON = "event_too_soon"


def to_cents(amount):
    """Round a Decimal half-up to cents."""
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class LineRequest:
    """One cart line as the customer asked for it: a variant or a package, and how many."""

    quantity: int
    variant_id: int | None = None
    package_id: int | None = None


@dataclass(frozen=True)
class QuoteLine:
    """One priced line of a quote."""

    kind: str
    name: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal
    available: bool
    variant_id: int | None = None
    package_id: int | None = None


@dataclass(frozen=True)
class QuoteError:
    """A reason the quote cannot be booked as it stands."""

    code: str
    message: str


@dataclass(frozen=True)
class Quote:
    """A priced cart for one event date and delivery ZIP.

    rental_total is what the booking will owe (Booking.total); total adds the
    refundable caution, which is charged alongside but tracked separately.
    """

    event_date: date
    zip_code: str
    lines: tuple[QuoteLine, ...]
    items_subtotal: Decimal
    package_subtotal: Decimal
    discount: Decimal
    delivery_fee: Decimal
    setup_fee: Decimal
    tax: Decimal
    caution: Decimal
    rental_total: Decimal
    total: Decimal
    deposit_amount: Decimal
    errors: tuple[QuoteError, ...] = ()
    promo: PromoCode | None = None
    zone: DeliveryZone | None = None
    variant_quantities: dict = field(default_factory=dict, compare=False, repr=False)
    package_variant_quantities: dict = field(default_factory=dict, compare=False, repr=False)
    variants: dict = field(default_factory=dict, compare=False, repr=False)
    packages: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def is_bookable(self):
        """True when the quote has no errors and can become a booking."""
        return not self.errors


class QuoteNotBookable(Exception):
    """Raised when a booking is requested for a quote that has errors."""

    def __init__(self, quote):
        self.quote = quote
        super().__init__("; ".join(error.message for error in quote.errors))


def zone_for_zip(zip_code):
    """Return the active delivery zone serving a ZIP, or None."""
    cleaned = normalise_zip(zip_code)
    if not cleaned:
        return None
    return DeliveryZone.objects.filter(is_active=True, zip_codes__contains=[cleaned]).first()


def promo_problem(promo, subtotal, now=None):
    """Return why a promo code cannot be used on this subtotal, or None if it can."""
    now = now or timezone.now()
    if not promo.is_active:
        return "This promo code is not active."
    if promo.valid_from and now < promo.valid_from:
        return "This promo code is not valid yet."
    if promo.valid_until and now > promo.valid_until:
        return "This promo code has expired."
    if promo.is_used_up:
        return "This promo code has been used the maximum number of times."
    if subtotal < promo.min_subtotal:
        return f"This promo code needs a subtotal of at least ${promo.min_subtotal}."
    return None


def promo_discount(promo, subtotal):
    """The discount a promo gives on an items-and-packages subtotal, never above it."""
    if promo.percent_off is not None:
        discount = to_cents(subtotal * promo.percent_off / HUNDRED)
    else:
        discount = promo.amount_off
    return min(discount, subtotal)


def _merge_requests(lines):
    """Return ({variant_id: quantity}, {package_id: quantity}) in first-seen order.

    Accepts LineRequest objects or mappings. Any other keys on a mapping, such
    as prices sent back by a browser, are ignored.
    """
    variant_quantities, package_quantities = {}, {}
    for line in lines:
        if isinstance(line, Mapping):
            line = LineRequest(
                quantity=line.get("quantity"),
                variant_id=line.get("variant_id"),
                package_id=line.get("package_id"),
            )
        if (line.variant_id is None) == (line.package_id is None):
            raise ValueError("Each line needs either a variant or a package.")
        if isinstance(line.quantity, bool) or not isinstance(line.quantity, int):
            raise ValueError("Each line needs a whole-number quantity.")
        if line.quantity < 1:
            raise ValueError("Each line needs a quantity of at least 1.")
        if line.variant_id is not None:
            target, key = variant_quantities, int(line.variant_id)
        else:
            target, key = package_quantities, int(line.package_id)
        target[key] = target.get(key, 0) + line.quantity
    return variant_quantities, package_quantities


def _package_is_rentable(package):
    components = package.items.all()
    return (
        package.is_active
        and bool(components)
        and all(component.variant.item.is_active for component in components)
    )


def build_quote(lines, event_date, zip_code, promo_code=None):
    """Price a cart for an event date and delivery ZIP.

    lines is an iterable of LineRequest (or mappings with variant_id or
    package_id, and quantity). Always returns a Quote; anything that stops it
    being booked is listed in quote.errors.
    """
    item_requests, package_requests = _merge_requests(lines)
    errors = []
    if not item_requests and not package_requests:
        errors.append(QuoteError(EMPTY_CART, "Add at least one item to get a quote."))
    notice = availability.notice_problem(event_date)
    if notice:
        errors.append(QuoteError(EVENT_TOO_SOON, notice))

    packages = {
        package.pk: package
        for package in Package.objects.filter(pk__in=package_requests).prefetch_related(
            "items__variant__item"
        )
    }
    variants = {
        variant.pk: variant
        for variant in ItemVariant.objects.filter(pk__in=item_requests).select_related("item")
    }
    package_variant_quantities = defaultdict(int)
    for package_id, quantity in package_requests.items():
        package = packages.get(package_id)
        if package is None:
            continue
        for component in package.items.all():
            variants.setdefault(component.variant_id, component.variant)
            package_variant_quantities[component.variant_id] += component.quantity * quantity

    needed = defaultdict(int, package_variant_quantities)
    for variant_id, quantity in item_requests.items():
        if variant_id in variants:
            needed[variant_id] += quantity
    in_stock = availability.available_quantities(variants.values(), event_date)
    short = {variant_id for variant_id, wanted in needed.items() if wanted > in_stock[variant_id]}

    quote_lines = []
    for variant_id, quantity in item_requests.items():
        variant = variants.get(variant_id)
        if variant is None:
            errors.append(QuoteError(LINE_UNAVAILABLE, "An item in the cart no longer exists."))
            continue
        available = variant.item.is_active and variant_id not in short
        if not available:
            errors.append(_unavailable(str(variant), event_date))
        quote_lines.append(
            QuoteLine(
                kind=ITEM,
                name=str(variant),
                quantity=quantity,
                unit_price=variant.unit_price,
                line_total=to_cents(variant.unit_price * quantity),
                available=available,
                variant_id=variant_id,
            )
        )
    for package_id, quantity in package_requests.items():
        package = packages.get(package_id)
        if package is None:
            errors.append(QuoteError(LINE_UNAVAILABLE, "A package in the cart no longer exists."))
            continue
        component_ids = {component.variant_id for component in package.items.all()}
        available = _package_is_rentable(package) and not (component_ids & short)
        if not available:
            errors.append(_unavailable(package.name, event_date))
        quote_lines.append(
            QuoteLine(
                kind=PACKAGE,
                name=package.name,
                quantity=quantity,
                unit_price=package.package_price,
                line_total=to_cents(package.package_price * quantity),
                available=available,
                package_id=package_id,
            )
        )

    items_subtotal = sum((line.line_total for line in quote_lines if line.kind == ITEM), ZERO)
    package_subtotal = sum((line.line_total for line in quote_lines if line.kind == PACKAGE), ZERO)
    subtotal = items_subtotal + package_subtotal

    promo, discount = None, ZERO
    code = (promo_code or "").strip()
    if code:
        candidate = PromoCode.objects.filter(code__iexact=code).first()
        problem = (
            "This promo code is not recognised."
            if candidate is None
            else promo_problem(candidate, subtotal)
        )
        if problem:
            errors.append(QuoteError(PROMO_INVALID, problem))
        else:
            promo, discount = candidate, promo_discount(candidate, subtotal)

    cleaned_zip = normalise_zip(zip_code)
    zone = zone_for_zip(cleaned_zip)
    if zone is None:
        errors.append(QuoteError(ZIP_NOT_SERVED, f"We do not deliver to ZIP code {zip_code} yet."))
    delivery_fee = zone.delivery_fee if zone else ZERO
    setup_fee = zone.setup_fee if zone else ZERO

    tax = sales_tax(subtotal, discount, delivery_fee, setup_fee)
    caution = caution_for(
        (variants[variant_id].item.caution_fee, quantity) for variant_id, quantity in needed.items()
    )
    rental_total = subtotal - discount + delivery_fee + setup_fee + tax
    deposit = to_cents(rental_total * Decimal(settings.DEPOSIT_PERCENT) / HUNDRED)

    return Quote(
        event_date=event_date,
        zip_code=cleaned_zip,
        lines=tuple(quote_lines),
        items_subtotal=items_subtotal,
        package_subtotal=package_subtotal,
        discount=discount,
        delivery_fee=delivery_fee,
        setup_fee=setup_fee,
        tax=tax,
        caution=caution,
        rental_total=rental_total,
        total=rental_total + caution,
        deposit_amount=deposit,
        errors=tuple(errors),
        promo=promo,
        zone=zone,
        variant_quantities=dict(needed),
        package_variant_quantities=dict(package_variant_quantities),
        variants=variants,
        packages=packages,
    )


def sales_tax(subtotal, discount, delivery_fee, setup_fee):
    """Sales tax on items and packages after discount, plus delivery and setup if taxed."""
    taxable = max(subtotal - discount, ZERO)
    if settings.TAX_DELIVERY:
        taxable += delivery_fee
    if settings.TAX_SETUP:
        taxable += setup_fee
    return to_cents(taxable * Decimal(settings.SALES_TAX_RATE) / HUNDRED)


def caution_for(fees_and_quantities):
    """Total caution for (caution fee per unit, units) pairs, every unit reserved counting."""
    return to_cents(sum((fee * quantity for fee, quantity in fees_and_quantities), ZERO))


def booking_tax(booking):
    """Sales tax on a booking's current lines, fees and discount; zero when it is tax exempt."""
    if booking.tax_exempt:
        return ZERO
    items_subtotal = sum(
        (
            to_cents(item.unit_price * (item.quantity - item.package_quantity))
            for item in booking.items.all()
        ),
        ZERO,
    )
    package_subtotal = sum(
        (to_cents(line.unit_price * line.quantity) for line in booking.packages.all()), ZERO
    )
    return sales_tax(
        items_subtotal + package_subtotal,
        booking.discount,
        booking.delivery_fee,
        booking.setup_fee,
    )


def booking_caution(booking):
    """Caution owed for a booking's current lines: every unit reserved counts."""
    items = booking.items.select_related("variant__item")
    return caution_for((item.variant.item.caution_fee, item.quantity) for item in items)


def reprice_booking(booking):
    """Recompute a booking's tax and caution from its current lines and save them.

    For bookings edited by staff before any payment: lines, fees and discount
    may have changed, so tax and caution follow them. Once money is on the
    booking its lines change only through lifecycle.change_item, which
    recomputes them the same way.
    """
    booking.tax = booking_tax(booking)
    booking.caution_fee = booking_caution(booking)
    booking.save(update_fields=["tax", "caution_fee", "updated_at"])
    return booking


def _unavailable(name, event_date):
    return QuoteError(LINE_UNAVAILABLE, f"{name} is not available on {event_date:%b %d, %Y}.")


def create_booking_from_quote(
    lines,
    event_date,
    zip_code,
    promo_code=None,
    *,
    customer_name,
    email,
    phone,
    delivery_address,
):
    """Re-price the cart and create a held booking for it, or raise QuoteNotBookable.

    Takes the cart, never prices: every amount on the booking comes from a
    quote built here. Stock is reserved through availability.reserve(), which
    locks the variants and checks them again, so a cart that sold out since it
    was quoted is refused.
    """
    with transaction.atomic():
        quote = build_quote(lines, event_date, zip_code, promo_code)
        if not quote.is_bookable:
            raise QuoteNotBookable(quote)

        try:
            booking = availability.reserve(
                [
                    (quote.variants[variant_id], quantity)
                    for variant_id, quantity in quote.variant_quantities.items()
                ],
                event_date,
                customer_name=customer_name,
                email=email,
                phone=phone,
                delivery_address=delivery_address,
            )
        except availability.InsufficientStockError:
            raise QuoteNotBookable(build_quote(lines, event_date, zip_code, promo_code)) from None

        items = list(booking.items.all())
        for item in items:
            item.unit_price = quote.variants[item.variant_id].unit_price
            item.package_quantity = quote.package_variant_quantities.get(item.variant_id, 0)
        BookingItem.objects.bulk_update(items, ["unit_price", "package_quantity"])
        BookingPackage.objects.bulk_create(
            BookingPackage(
                booking=booking,
                package_id=line.package_id,
                name=line.name,
                quantity=line.quantity,
                unit_price=line.unit_price,
            )
            for line in quote.lines
            if line.kind == PACKAGE
        )

        booking.zip_code = quote.zip_code
        booking.delivery_fee = quote.delivery_fee
        booking.setup_fee = quote.setup_fee
        booking.discount = quote.discount
        booking.tax = quote.tax
        booking.caution_fee = quote.caution
        booking.promo_code = quote.promo
        booking.save()
        if booking.total != quote.rental_total:
            raise RuntimeError(
                f"Booking total ${booking.total} does not match its quote ${quote.rental_total}."
            )
    return booking
