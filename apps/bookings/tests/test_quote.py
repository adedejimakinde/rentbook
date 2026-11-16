"""Quote builder: totals, tax, caution, zones, promo codes, rounding and booking creation."""

from datetime import timedelta
from decimal import Decimal as D

import pytest
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.services import availability, quote
from apps.bookings.services.quote import LineRequest, build_quote, create_booking_from_quote
from apps.bookings.tests.factories import (
    EVENT_DATE,
    BookingItemFactory,
    DeliveryZoneFactory,
    ItemVariantFactory,
    PackageFactory,
    PackageItemFactory,
    PromoCodeFactory,
    StockAdjustmentFactory,
)

pytestmark = pytest.mark.django_db

ZIP = "75001"
CUSTOMER = {
    "customer_name": "Ada Planner",
    "email": "ada@example.com",
    "phone": "555-0100",
    "delivery_address": "1 Main St, Dallas, TX 75001",
}


@pytest.fixture(autouse=True)
def pricing(settings):
    """No tax, 50% deposit, and one zone: $40 delivery plus $20 setup."""
    settings.SALES_TAX_RATE = D("0")
    settings.TAX_DELIVERY = False
    settings.TAX_SETUP = False
    settings.DEPOSIT_PERCENT = 50
    return DeliveryZoneFactory(zip_codes=[ZIP], delivery_fee=D("40.00"), setup_fee=D("20.00"))


def make_variant(price, stock=100, caution="0.00", name=None):
    extra = {"item__name": name} if name else {}
    return ItemVariantFactory(
        item__base_price=D(price), item__caution_fee=D(caution), stock_quantity=stock, **extra
    )


def item_line(variant, quantity):
    return LineRequest(variant_id=variant.pk, quantity=quantity)


def package_line(package, quantity=1):
    return LineRequest(package_id=package.pk, quantity=quantity)


def party_package(price="180.00", chair_stock=100, chair_caution="0.00"):
    """A package of 4 tables and 32 chairs. Returns (package, tables, chairs)."""
    tables = make_variant("12.00", stock=10)
    chairs = make_variant("2.50", stock=chair_stock, caution=chair_caution)
    package = PackageFactory(name="Party Pack", package_price=D(price))
    PackageItemFactory(package=package, variant=tables, quantity=4)
    PackageItemFactory(package=package, variant=chairs, quantity=32)
    return package, tables, chairs


def codes(result):
    return [error.code for error in result.errors]


# Totals


def test_items_quote_with_tax_and_caution(settings):
    settings.SALES_TAX_RATE = D("8.25")
    chairs = make_variant("2.50", name="Folding Chair")
    tent = make_variant("150.00", stock=2, caution="50.00")

    result = build_quote([item_line(chairs, 40), item_line(tent, 1)], EVENT_DATE, ZIP)

    assert result.is_bookable
    first = result.lines[0]
    assert (first.name, first.quantity, first.unit_price, first.line_total, first.available) == (
        "Folding Chair (Default)",
        40,
        D("2.50"),
        D("100.00"),
        True,
    )
    assert result.items_subtotal == D("250.00")
    assert result.package_subtotal == D("0.00")
    assert result.discount == D("0.00")
    assert (result.delivery_fee, result.setup_fee) == (D("40.00"), D("20.00"))
    assert result.tax == D("20.63")
    assert result.caution == D("50.00")
    assert result.rental_total == D("330.63")
    assert result.total == D("380.63")
    assert result.deposit_amount == D("165.32")


def test_package_quote_with_tax_and_caution(settings):
    settings.SALES_TAX_RATE = D("8.25")
    package, _, _ = party_package(chair_caution="0.50")

    result = build_quote([package_line(package, 2)], EVENT_DATE, ZIP)

    assert result.is_bookable
    line = result.lines[0]
    assert (line.kind, line.name, line.quantity, line.line_total) == (
        "package",
        "Party Pack",
        2,
        D("360.00"),
    )
    assert result.items_subtotal == D("0.00")
    assert result.package_subtotal == D("360.00")
    assert result.tax == D("29.70")
    assert result.caution == D("32.00")
    assert result.rental_total == D("449.70")
    assert result.total == D("481.70")


def test_mixed_cart_quote(settings):
    settings.SALES_TAX_RATE = D("10")
    package, _, chairs = party_package(chair_caution="0.50")

    result = build_quote([item_line(chairs, 10), package_line(package)], EVENT_DATE, ZIP)

    assert result.is_bookable
    assert result.items_subtotal == D("25.00")
    assert result.package_subtotal == D("180.00")
    assert result.tax == D("20.50")
    assert result.caution == D("21.00")
    assert result.rental_total == D("285.50")
    assert result.total == D("306.50")
    assert result.deposit_amount == D("142.75")


def test_repeated_lines_for_one_variant_are_merged():
    chairs = make_variant("2.50")

    result = build_quote([item_line(chairs, 10), item_line(chairs, 5)], EVENT_DATE, ZIP)

    assert [line.quantity for line in result.lines] == [15]
    assert result.items_subtotal == D("37.50")


def test_empty_cart_and_bad_lines():
    assert codes(build_quote([], EVENT_DATE, ZIP)) == [quote.EMPTY_CART]
    with pytest.raises(ValueError, match="at least 1"):
        build_quote([LineRequest(variant_id=1, quantity=0)], EVENT_DATE, ZIP)
    with pytest.raises(ValueError, match="either a variant or a package"):
        build_quote([LineRequest(quantity=1)], EVENT_DATE, ZIP)


@pytest.mark.parametrize(
    ("tax_delivery", "tax_setup", "expected"),
    [
        (False, False, "10.00"),
        (True, False, "14.00"),
        (False, True, "12.00"),
        (True, True, "16.00"),
    ],
)
def test_delivery_and_setup_are_taxed_only_when_switched_on(
    settings, tax_delivery, tax_setup, expected
):
    settings.SALES_TAX_RATE = D("10")
    settings.TAX_DELIVERY = tax_delivery
    settings.TAX_SETUP = tax_setup
    tent = make_variant("100.00", caution="75.00")

    result = build_quote([item_line(tent, 1)], EVENT_DATE, ZIP)

    assert result.tax == D(expected)
    assert result.rental_total == D("160.00") + D(expected)


# Rounding


def test_tax_is_rounded_once_on_the_total_not_per_line(settings):
    settings.SALES_TAX_RATE = D("8.25")
    lines = [item_line(make_variant("10.10"), 1) for _ in range(3)]

    result = build_quote(lines, EVENT_DATE, ZIP)

    assert result.items_subtotal == D("30.30")
    assert result.tax == D("2.50")  # 0.83 per line would add up to 2.49
    assert result.rental_total == D("92.80")


def test_percent_discount_is_rounded_once_on_the_subtotal():
    PromoCodeFactory(code="HALF", percent_off=D("50"))
    lines = [item_line(make_variant("0.33"), 1) for _ in range(3)]

    result = build_quote(lines, EVENT_DATE, ZIP, "HALF")

    assert result.discount == D("0.50")  # 0.17 per line would add up to 0.51
    assert result.rental_total == D("60.49")


# Delivery zones


def test_zip_not_served():
    chairs = make_variant("2.50")

    result = build_quote([item_line(chairs, 4)], EVENT_DATE, "90210")

    assert codes(result) == [quote.ZIP_NOT_SERVED]
    assert "90210" in result.errors[0].message
    assert (result.delivery_fee, result.setup_fee) == (D("0.00"), D("0.00"))


def test_inactive_zone_is_not_served(pricing):
    pricing.is_active = False
    pricing.save()

    result = build_quote([item_line(make_variant("2.50"), 4)], EVENT_DATE, ZIP)

    assert codes(result) == [quote.ZIP_NOT_SERVED]


def test_zip_plus_four_and_spaces_are_accepted():
    result = build_quote([item_line(make_variant("2.50"), 4)], EVENT_DATE, " 75001-1234 ")

    assert result.is_bookable
    assert result.zip_code == "75001"


# Promo codes


def test_percent_promo_reduces_subtotal_and_tax_base(settings):
    settings.SALES_TAX_RATE = D("10")
    PromoCodeFactory(code="SPRING10", percent_off=D("10"))
    tent = make_variant("250.00")

    result = build_quote([item_line(tent, 1)], EVENT_DATE, ZIP, "spring10")

    assert result.is_bookable
    assert result.promo.code == "SPRING10"
    assert result.discount == D("25.00")
    assert result.tax == D("22.50")
    assert result.rental_total == D("307.50")


def test_fixed_amount_promo_applies_to_items_and_packages():
    PromoCodeFactory(code="TAKE30", percent_off=None, amount_off=D("30.00"))
    package, _, chairs = party_package()

    result = build_quote([item_line(chairs, 10), package_line(package)], EVENT_DATE, ZIP, "TAKE30")

    assert result.discount == D("30.00")
    assert result.rental_total == D("235.00")


def promo_error(code, subtotal_price="100.00"):
    result = build_quote([item_line(make_variant(subtotal_price), 1)], EVENT_DATE, ZIP, code)
    assert codes(result) == [quote.PROMO_INVALID]
    assert result.discount == D("0.00")
    assert result.promo is None
    return result.errors[0].message


def test_expired_promo_is_rejected_with_reason():
    PromoCodeFactory(code="OLD", valid_until=timezone.now() - timedelta(days=1))
    assert "expired" in promo_error("OLD")


def test_promo_not_started_inactive_or_unknown_is_rejected_with_reason():
    PromoCodeFactory(code="SOON", valid_from=timezone.now() + timedelta(days=1))
    PromoCodeFactory(code="OFF", is_active=False)
    assert "not valid yet" in promo_error("SOON")
    assert "not active" in promo_error("OFF")
    assert "not recognised" in promo_error("NOPE")


def test_promo_min_subtotal():
    PromoCodeFactory(code="BIG", min_subtotal=D("100.01"))
    assert "at least $100.01" in promo_error("BIG")

    result = build_quote([item_line(make_variant("100.01"), 1)], EVENT_DATE, ZIP, "BIG")
    assert result.is_bookable


def test_promo_max_uses_is_checked_at_quote_time():
    PromoCodeFactory(code="FIRST5", max_uses=5, uses_count=5)
    assert "maximum number of times" in promo_error("FIRST5")


def test_promo_cannot_take_the_subtotal_below_zero(settings):
    settings.SALES_TAX_RATE = D("10")
    PromoCodeFactory(code="HUGE", percent_off=None, amount_off=D("500.00"))
    tent = make_variant("100.00", caution="75.00")

    result = build_quote([item_line(tent, 1)], EVENT_DATE, ZIP, "HUGE")

    assert result.discount == D("100.00")
    assert result.tax == D("0.00")
    assert result.rental_total == D("60.00")
    assert result.total == D("135.00")


def test_promo_is_not_applied_to_delivery_tax_or_caution(settings):
    settings.SALES_TAX_RATE = D("10")
    settings.TAX_DELIVERY = True
    PromoCodeFactory(code="FREE", percent_off=D("100"))
    tent = make_variant("100.00", caution="75.00")

    result = build_quote([item_line(tent, 1)], EVENT_DATE, ZIP, "FREE")

    assert result.discount == D("100.00")
    assert (result.delivery_fee, result.setup_fee) == (D("40.00"), D("20.00"))
    assert result.tax == D("4.00")
    assert result.caution == D("75.00")
    assert result.rental_total == D("64.00")
    assert result.total == D("139.00")


def test_building_a_quote_does_not_use_up_the_promo():
    promo = PromoCodeFactory(code="ONCE", max_uses=1)
    for _ in range(3):
        assert build_quote(
            [item_line(make_variant("50.00"), 1)], EVENT_DATE, ZIP, "ONCE"
        ).is_bookable
    promo.refresh_from_db()
    assert promo.uses_count == 0


# Availability


def test_unavailable_line_is_reported_and_booking_is_refused():
    chairs = make_variant("2.50", stock=10, name="Folding Chair")
    tent = make_variant("150.00", stock=2)
    BookingItemFactory(variant=chairs, quantity=8, booking__status=Booking.Status.PAID)
    lines = [item_line(chairs, 3), item_line(tent, 1)]

    result = build_quote(lines, EVENT_DATE, ZIP)

    assert [line.available for line in result.lines] == [False, True]
    assert codes(result) == [quote.LINE_UNAVAILABLE]
    assert "Folding Chair (Default) is not available" in result.errors[0].message
    assert result.items_subtotal == D("157.50")

    before = Booking.objects.count()
    with pytest.raises(quote.QuoteNotBookable) as raised:
        create_booking_from_quote(lines, EVENT_DATE, ZIP, **CUSTOMER)
    assert codes(raised.value.quote) == [quote.LINE_UNAVAILABLE]
    assert Booking.objects.count() == before


def test_item_line_and_package_compete_for_the_same_stock():
    package, _, chairs = party_package(chair_stock=40)

    enough = build_quote([item_line(chairs, 8), package_line(package)], EVENT_DATE, ZIP)
    short = build_quote([item_line(chairs, 9), package_line(package)], EVENT_DATE, ZIP)

    assert enough.is_bookable
    assert [line.available for line in short.lines] == [False, False]


def test_damaged_stock_inactive_items_and_inactive_packages_are_unavailable():
    damaged = make_variant("10.00", stock=2)
    StockAdjustmentFactory(variant=damaged, quantity=1)
    retired = make_variant("10.00")
    retired.item.is_active = False
    retired.item.save()
    package, _, _ = party_package()
    package.is_active = False
    package.save()

    result = build_quote(
        [item_line(damaged, 2), item_line(retired, 1), package_line(package)], EVENT_DATE, ZIP
    )

    assert [line.available for line in result.lines] == [False, False, False]


def test_booking_is_refused_for_a_bad_zip_or_promo():
    chairs = make_variant("2.50")
    with pytest.raises(quote.QuoteNotBookable):
        create_booking_from_quote([item_line(chairs, 1)], EVENT_DATE, "90210", **CUSTOMER)
    with pytest.raises(quote.QuoteNotBookable, match="not recognised"):
        create_booking_from_quote([item_line(chairs, 1)], EVENT_DATE, ZIP, "NOPE", **CUSTOMER)
    assert Booking.objects.count() == 0


# Booking creation


def test_booking_snapshots_the_quote_and_reserves_package_stock(settings):
    settings.SALES_TAX_RATE = D("10")
    promo = PromoCodeFactory(code="TAKE5", percent_off=None, amount_off=D("5.00"))
    package, tables, chairs = party_package(chair_caution="0.50")
    lines = [item_line(chairs, 10), package_line(package)]
    quoted = build_quote(lines, EVENT_DATE, ZIP, "take5")

    booking = create_booking_from_quote(lines, EVENT_DATE, ZIP, "take5", **CUSTOMER)

    booking.refresh_from_db()
    assert booking.status == Booking.Status.HELD
    assert booking.zip_code == ZIP
    assert booking.promo_code == promo
    assert booking.promo_redeemed_at is None
    assert (booking.delivery_fee, booking.setup_fee) == (D("40.00"), D("20.00"))
    assert booking.discount == D("5.00")
    assert booking.tax == D("20.00")
    assert booking.caution_fee == D("21.00")
    assert booking.total == quoted.rental_total == D("280.00")
    assert booking.deposit_amount == quoted.deposit_amount == D("140.00")

    reserved = {item.variant_id: item for item in booking.items.all()}
    assert (reserved[chairs.pk].quantity, reserved[chairs.pk].package_quantity) == (42, 32)
    assert (reserved[tables.pk].quantity, reserved[tables.pk].package_quantity) == (4, 4)
    held_package = booking.packages.get()
    assert (held_package.name, held_package.quantity, held_package.unit_price) == (
        "Party Pack",
        1,
        D("180.00"),
    )
    assert availability.available_quantity(chairs, EVENT_DATE) == 58
    assert availability.available_quantity(tables, EVENT_DATE) == 6


def test_booking_ignores_numbers_sent_by_the_caller(settings):
    settings.SALES_TAX_RATE = D("10")
    tent = make_variant("150.00", caution="50.00")
    tampered = [
        {
            "variant_id": tent.pk,
            "quantity": 2,
            "unit_price": "0.01",
            "line_total": "0.02",
            "tax": "0.00",
            "total": "0.02",
        }
    ]

    booking = create_booking_from_quote(tampered, EVENT_DATE, ZIP, **CUSTOMER)

    assert booking.items.get().unit_price == D("150.00")
    assert booking.tax == D("30.00")
    assert booking.caution_fee == D("100.00")
    assert booking.total == D("390.00")


def test_booking_uses_current_prices_not_the_ones_quoted_earlier():
    tent = make_variant("150.00")
    lines = [item_line(tent, 1)]
    assert build_quote(lines, EVENT_DATE, ZIP).rental_total == D("210.00")
    tent.item.base_price = D("175.00")
    tent.item.save()

    booking = create_booking_from_quote(lines, EVENT_DATE, ZIP, **CUSTOMER)

    assert booking.total == D("235.00")
