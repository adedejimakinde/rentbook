"""Public catalogue pages: status, pagination, filtering, availability and price wording."""

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.bookings.tests.factories import (
    BookingFactory,
    BookingItemFactory,
    CategoryFactory,
    ItemFactory,
    ItemVariantFactory,
    PackageFactory,
    PackageItemFactory,
)
from apps.catalogue.admin import ItemAdminForm
from apps.catalogue.models import Item

pytestmark = pytest.mark.django_db


def make_item(stock=10, confirmed=True, **kwargs):
    variant = ItemVariantFactory(
        item=ItemFactory(**kwargs), stock_quantity=stock, stock_confirmed=confirmed
    )
    return variant.item, variant


def book(variant, quantity, event_date):
    booking = BookingFactory(event_date=event_date)
    BookingItemFactory(booking=booking, variant=variant, quantity=quantity)


def test_every_public_page_returns_200(client):
    item, variant = make_item()
    package = PackageFactory()
    PackageItemFactory(package=package, variant=variant)
    urls = [
        "/",
        "/rentals/",
        item.category.get_absolute_url(),
        item.get_absolute_url(),
        f"{item.get_absolute_url()}availability/",
        "/packages/",
        package.get_absolute_url(),
        "/gallery/",
        "/about/",
        "/faq/",
        "/contact/",
        "/links/",
        "/sitemap.xml",
        "/robots.txt",
    ]

    for url in urls:
        assert client.get(url).status_code == 200, url


def test_unknown_category_item_and_empty_package_are_404(client):
    package = PackageFactory()

    assert client.get("/rentals/nope/").status_code == 404
    assert client.get("/items/nope/").status_code == 404
    assert client.get(package.get_absolute_url()).status_code == 404


def test_every_page_has_a_unique_title_and_description(client):
    item, variant = make_item()
    package = PackageFactory()
    PackageItemFactory(package=package, variant=variant)
    urls = [
        "/",
        "/rentals/",
        item.category.get_absolute_url(),
        item.get_absolute_url(),
        "/packages/",
        package.get_absolute_url(),
        "/gallery/",
        "/about/",
        "/faq/",
        "/contact/",
        "/links/",
    ]
    titles, descriptions = set(), set()

    for url in urls:
        html = client.get(url).content.decode()
        title = html.split("<title>")[1].split("</title>")[0]
        description = html.split('name="description" content="')[1].split('"')[0]
        assert 'property="og:title"' in html and 'property="og:description"' in html
        titles.add(title)
        descriptions.add(description)

    assert len(titles) == len(urls)
    assert len(descriptions) == len(urls)


def test_home_has_headline_tagline_and_local_business_data(client):
    html = client.get("/").content.decode()

    assert "Party rentals across DFW, delivered and set up" in html
    assert "Your one-stop shop for event rentals" in html
    assert "Check availability" in html
    assert '"@type": "LocalBusiness"' in html
    assert "Dallas-Fort Worth" in html


def test_catalogue_paginates_twelve_per_page(client):
    category = CategoryFactory()
    for _ in range(30):
        ItemVariantFactory(item=ItemFactory(category=category))

    def cards(url):
        return client.get(url).content.decode().count("<article")

    assert cards("/rentals/") == 12
    assert cards("/rentals/?page=2") == 12
    assert cards("/rentals/?page=3") == 6
    assert "Page 1 of 3" in client.get("/rentals/").content.decode()
    assert client.get("/rentals/?page=99").status_code == 200


def test_category_filter_limits_items_and_marks_current_chip(client):
    chairs, tables = (
        CategoryFactory(name="Chairs", slug="chairs"),
        CategoryFactory(name="Tables", slug="tables"),
    )
    ItemVariantFactory(item=ItemFactory(category=chairs, name="Garden chair", slug="garden"))
    ItemVariantFactory(item=ItemFactory(category=tables, name="Round table", slug="round"))

    html = client.get("/rentals/chairs/").content.decode()

    assert "Garden chair" in html
    assert "Round table" not in html
    assert 'aria-current="page"' in html


def test_htmx_requests_get_partials_not_full_pages(client):
    item, _ = make_item()

    response = client.get("/rentals/", HTTP_HX_REQUEST="true")
    html = response.content.decode()

    assert "<html" not in html and item.name in html
    assert "HX-Request" in response["Vary"]


def test_catalogue_shows_availability_for_the_chosen_date(client, event_date):
    open_item, open_variant = make_item(name="Open item", slug="open")
    low_item, low_variant = make_item(stock=5, name="Low item", slug="low")
    gone_item, gone_variant = make_item(stock=2, name="Gone item", slug="gone")
    book(low_variant, 3, event_date)
    book(gone_variant, 2, event_date)

    html = client.get(f"/rentals/?date={event_date:%Y-%m-%d}").content.decode()

    assert "Available for this date" in html
    assert "Only 2 left for this date" in html
    assert "Unavailable for this date" in html
    assert f"{event_date:%A, %B} {event_date.day}" in html


def test_date_is_remembered_in_the_session(client, event_date):
    make_item()
    client.get(f"/rentals/?date={event_date:%Y-%m-%d}")

    html = client.get("/rentals/").content.decode()

    assert "Available for this date" in html


def test_date_clears_when_empty(client, event_date):
    make_item()
    client.get(f"/rentals/?date={event_date:%Y-%m-%d}")

    html = client.get("/rentals/?date=").content.decode()

    assert "Available for this date" not in html


def test_past_and_invalid_dates_show_an_error_and_no_availability(client):
    make_item()

    past = client.get("/rentals/?date=2020-01-01").content.decode()
    junk = client.get("/rentals/?date=banana").content.decode()

    assert "in the past" in past and "Available for this date" not in past
    assert "month, day and year" in junk


def test_unconfirmed_stock_says_call_to_confirm_not_unavailable(client, event_date):
    item, _ = make_item(stock=0, confirmed=False, name="Waterslide", slug="waterslide")

    catalogue = client.get(f"/rentals/?date={event_date:%Y-%m-%d}").content.decode()
    detail = client.get(f"{item.get_absolute_url()}?date={event_date:%Y-%m-%d}").content.decode()

    for html in (catalogue, detail):
        assert "Call to confirm availability" in html
        assert "Unavailable" not in html


def test_confirmed_zero_stock_still_says_unavailable(client, event_date):
    item, _ = make_item(stock=0, confirmed=True)

    html = client.get(f"{item.get_absolute_url()}?date={event_date:%Y-%m-%d}").content.decode()

    assert "Unavailable for this date" in html


def test_zero_price_says_call_for_price(client):
    item, _ = make_item(base_price=Decimal("0.00"))

    for url in ("/rentals/", item.get_absolute_url()):
        html = client.get(url).content.decode()
        assert "Call for price" in html
        assert "$0" not in html


def test_priced_item_shows_the_price_and_variants_show_from(client):
    item, _ = make_item(base_price=Decimal("25.00"))
    ItemVariantFactory(item=item, name="Gold", price_override=Decimal("35.00"))

    html = client.get("/rentals/").content.decode()

    assert "$25.00" in html and "From " in html
    assert "Call for price" not in html


def test_item_detail_availability_partial_responds_to_variant_and_quantity(client, event_date):
    item, black = make_item(stock=4, name="Garden chair", slug="garden-chair")
    white = ItemVariantFactory(item=item, name="White", stock_quantity=1)
    url = f"{item.get_absolute_url()}availability/"
    date = f"{event_date:%Y-%m-%d}"

    black_html = client.get(
        url, {"date": date, "variant": black.pk, "quantity": 2}
    ).content.decode()
    white_html = client.get(
        url, {"date": date, "variant": white.pk, "quantity": 3}
    ).content.decode()

    assert "<html" not in black_html
    assert "Only 4 left" in black_html or "Available" in black_html
    assert "Only 1 available for this date" in white_html


def test_item_detail_sections_and_placeholder_button(client):
    item, _ = make_item(
        name="Photo backdrop",
        slug="photo-backdrop",
        whats_included=["Backdrop stand", "Setup"],
        specs=[{"name": "Width", "value": "8 ft"}],
        description="A big backdrop.",
        short_description="Shiny.",
    )
    other = ItemFactory(category=item.category, name="Neon sign", slug="neon")
    ItemVariantFactory(item=other)

    html = client.get(item.get_absolute_url()).content.decode()

    assert "Backdrop stand" in html and "Width" in html and "8 ft" in html
    assert "Neon sign" in html
    assert 'disabled aria-disabled="true"' in html
    assert "Add to booking" in html
    assert 'for="item-date"' in html and 'for="item-qty"' in html


def test_inactive_items_are_hidden(client):
    item, _ = make_item(is_active=False, name="Retired")

    assert "Retired" not in client.get("/rentals/").content.decode()
    assert client.get(item.get_absolute_url()).status_code == 404


def test_package_pages_show_items_and_call_for_price(client):
    package = PackageFactory(name="Kids party", package_price=Decimal("0.00"))
    _, variant = make_item(name="Bounce house", slug="bounce-house")
    PackageItemFactory(package=package, variant=variant, quantity=2)

    listing = client.get("/packages/").content.decode()
    detail = client.get(package.get_absolute_url()).content.decode()

    assert "Kids party" in listing and "2 x Bounce house" in listing
    assert "Call for price" in listing and "Call for price" in detail


def test_catalogue_page_stays_within_ten_queries(client, event_date):
    category = CategoryFactory()
    for _ in range(12):
        item = ItemFactory(category=category)
        ItemVariantFactory(item=item)
        ItemVariantFactory(item=item, name="Alt")

    url = f"/rentals/?date={event_date:%Y-%m-%d}"
    with CaptureQueriesContext(connection) as first:
        client.get(url)
    with CaptureQueriesContext(connection) as repeat:
        client.get(url)

    assert len(repeat) <= 10, "steady state: reads only"
    assert len(first) <= 11, "first hit also writes the session (BEGIN, UPDATE, COMMIT)"


def test_home_and_item_pages_stay_within_ten_queries(client, event_date):
    item, _ = make_item(is_featured=True)
    for _ in range(6):
        ItemVariantFactory(item=ItemFactory(category=item.category, is_featured=True))
    client.get(f"/rentals/?date={event_date:%Y-%m-%d}")

    with CaptureQueriesContext(connection) as home:
        client.get("/")
    with CaptureQueriesContext(connection) as detail:
        client.get(item.get_absolute_url())

    assert len(home) <= 10 and len(detail) <= 10


def test_sitemap_lists_items_categories_packages_and_pages(client):
    item, variant = make_item(name="Sitemap chair", slug="sitemap-chair")
    hidden, _ = make_item(is_active=False, name="Hidden", slug="hidden-item")
    package = PackageFactory()
    PackageItemFactory(package=package, variant=variant)

    xml = client.get("/sitemap.xml").content.decode()

    assert "/items/sitemap-chair/" in xml
    assert "/items/hidden-item/" not in xml
    assert item.category.get_absolute_url() in xml
    assert package.get_absolute_url() in xml
    for path in ("/about/", "/faq/", "/contact/", "/rentals/", "/packages/"):
        assert path in xml


def test_robots_points_to_sitemap_and_hides_private_paths(client, settings):
    text = client.get("/robots.txt").content.decode()

    assert "Sitemap: " in text and "/sitemap.xml" in text
    assert "Disallow: /payments/" in text
    assert settings.ADMIN_URL not in text


def test_admin_form_parses_included_and_specs_text():
    item = ItemFactory()
    form = ItemAdminForm(
        data={
            "category": item.category_id,
            "name": item.name,
            "slug": item.slug,
            "base_price": "0",
            "caution_fee": "0",
            "is_active": "on",
            "whats_included_text": "Stand\n\n Setup \n",
            "specs_text": "Width: 8 ft\nPower: 110V: standard",
        },
        instance=item,
    )

    assert form.is_valid(), form.errors
    form.save()
    item = Item.objects.get(pk=item.pk)
    assert item.whats_included == ["Stand", "Setup"]
    assert item.specs == [
        {"name": "Width", "value": "8 ft"},
        {"name": "Power", "value": "110V: standard"},
    ]


def test_admin_form_rejects_spec_without_a_value():
    item = ItemFactory()
    form = ItemAdminForm(
        data={
            "category": item.category_id,
            "name": item.name,
            "slug": item.slug,
            "base_price": "0",
            "caution_fee": "0",
            "specs_text": "just words",
        },
        instance=item,
    )

    assert not form.is_valid()
    assert "specs_text" in form.errors


def test_categories_follow_sort_order_on_home_and_in_chips(client):
    for order, name in ((2, "Alpha"), (1, "Zulu")):
        category = CategoryFactory(name=name, slug=name.lower(), sort_order=order)
        ItemVariantFactory(item=ItemFactory(category=category))

    for url in ("/", "/rentals/"):
        html = client.get(url).content.decode()
        assert html.index("Zulu") < html.index("Alpha"), url
