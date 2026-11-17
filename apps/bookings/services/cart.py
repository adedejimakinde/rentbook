"""Session cart: the variants and packages a visitor wants, before they book.

The cart stores only ids and quantities. It never prices anything: every
amount shown comes from quote.build_quote, called through cart_quote().
"""

from dataclasses import dataclass, field

from apps.bookings.services import quote as quote_service
from apps.catalogue.models import ItemVariant, Package
from apps.core import site_settings
from apps.core.services import get_event_date, get_event_zip

CART_KEY = "cart"
PROMO_KEY = "cart_promo"
MAX_QUANTITY = 999

VARIANT = "variant"
PACKAGE = "package"
KINDS = (VARIANT, PACKAGE)

# Where each quote error is shown on the page.
ERROR_TARGETS = {
    quote_service.EVENT_TOO_SOON: "date",
    quote_service.ZIP_NOT_SERVED: "zip",
    quote_service.PROMO_INVALID: "promo",
}


class CartError(ValueError):
    """Raised when a cart change cannot be made (unknown item, bad quantity)."""


def _key(kind, pk):
    if kind not in KINDS:
        raise CartError("Unknown cart line.")
    try:
        return f"{kind}:{int(pk)}"
    except (TypeError, ValueError):
        raise CartError("Unknown cart line.") from None


def get_lines(request):
    """The cart as a list of {"kind", "id", "quantity"} dicts, in the order added."""
    lines = []
    for key, quantity in request.session.get(CART_KEY, {}).items():
        kind, _, pk = key.partition(":")
        if kind in KINDS and pk.isdigit() and isinstance(quantity, int) and quantity > 0:
            lines.append({"kind": kind, "id": int(pk), "quantity": quantity})
    return lines


def line_requests(lines):
    """Turn cart lines into the LineRequests build_quote takes."""
    return [
        quote_service.LineRequest(
            quantity=line["quantity"],
            variant_id=line["id"] if line["kind"] == VARIANT else None,
            package_id=line["id"] if line["kind"] == PACKAGE else None,
        )
        for line in lines
    ]


def count(request):
    """Number of lines in the cart. Reads the session only, no database."""
    return len(get_lines(request))


def parse_quantity(value):
    """A whole quantity between 1 and MAX_QUANTITY, or raise CartError."""
    try:
        quantity = int(str(value).strip())
    except (TypeError, ValueError):
        raise CartError("Enter a whole number.") from None
    if not 1 <= quantity <= MAX_QUANTITY:
        raise CartError(f"Choose a quantity from 1 to {MAX_QUANTITY}.")
    return quantity


def _check_bookable(kind, pk):
    """Refuse rentals that are inactive, gone, or have no price yet ("Call for price")."""
    if kind == VARIANT:
        rental = ItemVariant.objects.filter(pk=pk, item__is_active=True).select_related("item")
        rental = rental.first()
        price = rental.unit_price if rental else None
    else:
        rental = Package.objects.filter(pk=pk, is_active=True).first()
        price = rental.package_price if rental else None
    if rental is None:
        raise CartError("That rental is no longer available to book.")
    if not price:
        raise CartError("Call or WhatsApp us for a price on this rental.")


def _save(request, cart):
    request.session[CART_KEY] = cart
    request.session.modified = True


def add(request, kind, pk, quantity):
    """Add a quantity of a variant or package, merging with a line already in the cart."""
    quantity = parse_quantity(quantity)
    key = _key(kind, pk)
    _check_bookable(kind, int(pk))
    cart = dict(request.session.get(CART_KEY, {}))
    cart[key] = min(cart.get(key, 0) + quantity, MAX_QUANTITY)
    _save(request, cart)


def update(request, kind, pk, quantity):
    """Set a line's quantity. The line must already be in the cart."""
    quantity = parse_quantity(quantity)
    cart = dict(request.session.get(CART_KEY, {}))
    key = _key(kind, pk)
    if key not in cart:
        raise CartError("That line is not in your cart.")
    cart[key] = quantity
    _save(request, cart)


def remove(request, kind, pk):
    """Remove a line. Removing a line that is not there does nothing."""
    cart = dict(request.session.get(CART_KEY, {}))
    try:
        cart.pop(_key(kind, pk), None)
    except CartError:
        return
    _save(request, cart)


def clear(request):
    """Empty the cart and forget the promo code."""
    request.session.pop(CART_KEY, None)
    request.session.pop(PROMO_KEY, None)
    request.session.modified = True


def get_promo(request):
    """The promo code entered in the cart, or "" while promo codes are switched off."""
    if not site_settings.promo_codes_enabled():
        return ""
    return request.session.get(PROMO_KEY, "")


def set_promo(request, code):
    code = (code or "").strip()[:40]
    if code:
        request.session[PROMO_KEY] = code
    else:
        request.session.pop(PROMO_KEY, None)


@dataclass
class CartView:
    """Everything the cart and review pages show, built from one build_quote call."""

    lines: list
    event_date: object
    zip_code: str
    promo_code: str
    promo_enabled: bool = True
    quote: object = None
    errors: dict = field(default_factory=dict)
    line_errors: dict = field(default_factory=dict)

    @property
    def is_bookable(self):
        return self.quote is not None and self.quote.is_bookable


def _names(lines):
    """Names for cart lines when there is no quote yet (no date or ZIP chosen)."""
    variant_ids = [line["id"] for line in lines if line["kind"] == VARIANT]
    package_ids = [line["id"] for line in lines if line["kind"] == PACKAGE]
    variants = ItemVariant.objects.filter(pk__in=variant_ids).select_related("item")
    names = {(VARIANT, variant.pk): str(variant) for variant in variants}
    names.update(
        ((PACKAGE, pk), name)
        for pk, name in Package.objects.filter(pk__in=package_ids).values_list("pk", "name")
    )
    return names


def _quote_line_key(quote_line):
    if quote_line.kind == quote_service.ITEM:
        return (VARIANT, quote_line.variant_id)
    return (PACKAGE, quote_line.package_id)


def cart_view(request, event_date=None, zip_code=None):
    """The cart with its live quote for the session's event date and delivery ZIP.

    Without a date or ZIP there is no quote: lines are listed by name and the
    page asks for the missing values. Lines whose variant or package no longer
    exists are dropped from the cart.
    """
    lines = get_lines(request)
    event_date = event_date or get_event_date(request)
    zip_code = zip_code if zip_code is not None else get_event_zip(request)
    view = CartView(lines=[], event_date=event_date, zip_code=zip_code, promo_code="")
    view.promo_code = get_promo(request)
    view.promo_enabled = site_settings.promo_codes_enabled()
    if not lines:
        view.errors["general"] = ["Your cart is empty. Add rentals to get a quote."]
        return view

    if event_date is None or not zip_code:
        names = _names(lines)
        if event_date is None:
            view.errors["date"] = ["Choose your event date to see prices and availability."]
        if not zip_code:
            view.errors["zip"] = ["Enter your delivery ZIP code to see delivery and setup fees."]
        view.lines = [
            {**line, "name": names[(line["kind"], line["id"])]}
            for line in lines
            if (line["kind"], line["id"]) in names
        ]
        _prune(request, lines, names)
        return view

    quote = quote_service.build_quote(
        line_requests(lines), event_date, zip_code, view.promo_code or None
    )
    view.quote = quote
    by_key = {_quote_line_key(quote_line): quote_line for quote_line in quote.lines}
    view.lines = [
        {**line, "name": by_key[(line["kind"], line["id"])].name, "priced": by_key[key]}
        for line in lines
        if (key := (line["kind"], line["id"])) in by_key
    ]
    _prune(request, lines, by_key)
    _place_errors(view, quote)
    for line in view.lines:
        line["error"] = view.line_errors.get((line["kind"], line["id"]), "")
    return view


def _prune(request, lines, known):
    gone = [line for line in lines if (line["kind"], line["id"]) not in known]
    for line in gone:
        remove(request, line["kind"], line["id"])


def _place_errors(view, quote):
    """Put each quote error next to what it refers to: a line, the date, ZIP or promo code."""
    unavailable = {
        quote_service._unavailable(line.name, quote.event_date).message: _quote_line_key(line)
        for line in quote.lines
        if not line.available
    }
    for error in quote.errors:
        if error.code == quote_service.LINE_UNAVAILABLE and error.message in unavailable:
            view.line_errors[unavailable[error.message]] = error.message
            continue
        target = ERROR_TARGETS.get(error.code, "general")
        view.errors.setdefault(target, []).append(error.message)
