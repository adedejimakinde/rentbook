"""Catalogue sorting, the floating cart bar's running total, and card wording on phones."""

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.bookings.tests.factories import (
    CategoryFactory,
    ItemFactory,
    ItemVariantFactory,
    PackageFactory,
    PackageItemFactory,
)
from apps.catalogue.services import listing

pytestmark = pytest.mark.django_db


@pytest.fixture
def items():
    """Four items: a featured mid-price one, cheap, dear, and one with no online price."""
    category = CategoryFactory(name="Party", slug="party")
    made = {}
    for name, price, featured in (
        ("Bravo", "50.00", True),
        ("Alpha", "10.00", False),
        ("Charlie", "300.00", False),
        ("Delta", "0.00", False),
    ):
        item = ItemFactory(
            category=category,
            name=name,
            slug=name.lower(),
            base_price=Decimal(price),
            is_featured=featured,
        )
        made[name] = ItemVariantFactory(item=item, stock_quantity=5)
    return made


def names(sort):
    return [item.name for item in listing.catalogue_page(None, 1, sort).object_list]


def test_sorts_order_items_and_unpriced_items_go_last(items):
    assert names(listing.POPULAR) == ["Bravo", "Alpha", "Charlie", "Delta"]
    assert names(listing.PRICE_LOW) == ["Alpha", "Bravo", "Charlie", "Delta"]
    assert names(listing.PRICE_HIGH) == ["Charlie", "Bravo", "Alpha", "Delta"]
    assert names(listing.NAME) == ["Alpha", "Bravo", "Charlie", "Delta"]


def test_price_sort_uses_the_lowest_variant_price_the_card_shows(items):
    ItemVariantFactory(item=items["Charlie"].item, name="Small", price_override=Decimal("5.00"))

    assert names(listing.PRICE_LOW)[0] == "Charlie"
    assert items["Charlie"].item.display_price() == Decimal("5.00")


def test_unknown_sort_falls_back_to_popular():
    assert listing.clean_sort("drop table") == listing.POPULAR
    assert listing.clean_sort(listing.PRICE_HIGH) == listing.PRICE_HIGH


def test_sort_is_kept_in_chips_pagination_and_the_date_form(client, items):
    html = client.get("/rentals/?sort=price-high").content.decode()

    assert '<option value="price-high" selected>' in html
    assert 'href="/rentals/party/?sort=price-high"' in html
    assert 'name="sort" value="price-high"' in html
    assert html.index("Charlie") < html.index("Alpha")


def test_sort_control_has_a_label_and_updates_results_with_htmx(client, items):
    html = client.get("/rentals/").content.decode()

    assert 'for="catalogue-sort"' in html and 'id="catalogue-sort"' in html
    assert 'hx-target="#catalogue-results"' in html


def test_floating_cart_bar_shows_the_running_total(client, items):
    package = PackageFactory(package_price=Decimal("99.00"))
    PackageItemFactory(package=package, variant=items["Alpha"])
    for kind, pk, quantity in (("variant", items["Bravo"].pk, 3), ("package", package.pk, 1)):
        client.post(reverse("bookings:cart_add"), {"kind": kind, "id": pk, "quantity": quantity})

    with CaptureQueriesContext(connection) as queries:
        html = client.get("/rentals/").content.decode()

    assert "2 items in your booking" in html
    assert "$249.00" in html and "so far" in html
    assert len(queries) <= 10


def test_floating_cart_bar_is_absent_with_an_empty_cart(client, items):
    html = client.get("/rentals/").content.decode()

    assert "in your booking" not in html


def test_cards_show_short_availability_on_phones_and_full_text_for_screen_readers(
    client, items, event_date
):
    html = client.get(f"/rentals/?date={event_date:%Y-%m-%d}").content.decode()

    assert '<span class="md:hidden" aria-hidden="true">Available</span>' in html
    assert '<span class="max-md:sr-only">Available for this date</span>' in html
