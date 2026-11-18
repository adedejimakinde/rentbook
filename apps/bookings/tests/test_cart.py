"""Session cart: add, update and remove over HTMX, and the live quote it shows."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.bookings.services import cart as cart_service
from apps.bookings.services.quote import LineRequest, build_quote
from apps.bookings.tests.factories import ItemVariantFactory, PromoCodeFactory
from apps.bookings.tests.flow_helpers import HX, ZIP, add, catalogue, set_details

pytestmark = pytest.mark.django_db


@pytest.fixture
def items():
    return catalogue()


def session_cart(client):
    return client.session.get(cart_service.CART_KEY, {})


def money(value):
    return f"${value:,.2f}"


def test_add_variant_over_htmx_confirms_and_updates_header_count(client, items):
    response = add(client, "variant", items["chair"].pk, 4)

    html = response.content.decode()
    assert response.status_code == 200
    assert "Added to your booking" in html
    assert 'id="cart-count"' in html and 'hx-swap-oob="true"' in html
    assert "Booking cart, 1 item" in html
    assert session_cart(client) == {f"variant:{items['chair'].pk}": 4}


def test_adding_the_same_line_again_adds_to_its_quantity(client, items):
    add(client, "variant", items["chair"].pk, 4)
    add(client, "variant", items["chair"].pk, 6)
    add(client, "package", items["package"].pk, 1)

    assert session_cart(client) == {
        f"variant:{items['chair'].pk}": 10,
        f"package:{items['package'].pk}": 1,
    }


@pytest.mark.parametrize("quantity", ["0", "-2", "1000", "two", ""])
def test_add_refuses_bad_quantities(client, items, quantity):
    response = add(client, "variant", items["chair"].pk, quantity)

    assert response.status_code == 422
    assert session_cart(client) == {}


def test_add_refuses_inactive_or_unknown_rentals(client, items):
    items["chair"].item.is_active = False
    items["chair"].item.save()
    items["package"].is_active = False
    items["package"].save()

    for kind, pk in (("variant", items["chair"].pk), ("package", items["package"].pk)):
        assert add(client, kind, pk).status_code == 422
    assert add(client, "variant", 999999).status_code == 422
    assert add(client, "sofa", items["table"].pk).status_code == 422
    assert session_cart(client) == {}


def test_add_without_htmx_redirects_to_the_cart(client, items):
    response = add(client, "variant", items["chair"].pk, 2, htmx=False)

    assert response.status_code == 302 and response.url == reverse("bookings:cart")


def test_update_changes_quantity_and_returns_the_refreshed_quote(client, items, event_date):
    add(client, "variant", items["chair"].pk, 4)
    set_details(client, event_date)

    response = client.post(
        reverse("bookings:cart_update"),
        {"kind": "variant", "id": items["chair"].pk, "quantity": 20},
        headers=HX,
    )

    assert session_cart(client) == {f"variant:{items['chair'].pk}": 20}
    html = response.content.decode()
    assert "<html" not in html
    assert "20 x $3.50" in html and "$70.00" in html


def test_update_with_a_bad_quantity_keeps_the_line_and_says_why(client, items):
    add(client, "variant", items["chair"].pk, 4)

    response = client.post(
        reverse("bookings:cart_update"),
        {"kind": "variant", "id": items["chair"].pk, "quantity": 0},
        headers=HX,
    )

    assert "Choose a quantity from 1 to 999." in response.content.decode()
    assert session_cart(client) == {f"variant:{items['chair'].pk}": 4}


def test_update_of_a_line_not_in_the_cart_is_refused(client, items):
    response = client.post(
        reverse("bookings:cart_update"),
        {"kind": "variant", "id": items["chair"].pk, "quantity": 3},
        headers=HX,
    )

    assert "That line is not in your cart." in response.content.decode()
    assert session_cart(client) == {}


def test_remove_drops_the_line_and_updates_the_count(client, items):
    add(client, "variant", items["chair"].pk, 4)
    add(client, "package", items["package"].pk, 1)

    response = client.post(
        reverse("bookings:cart_remove"), {"kind": "variant", "id": items["chair"].pk}, headers=HX
    )

    assert session_cart(client) == {f"package:{items['package'].pk}": 1}
    assert "Booking cart, 1 item" in response.content.decode()


def test_cart_page_shows_exactly_the_quote_build_quote_gives(client, items, event_date, settings):
    settings.SALES_TAX_RATE = Decimal("8.25")
    settings.DEPOSIT_PERCENT = 50
    PromoCodeFactory(code="SPRING", percent_off=Decimal("10.00"))
    add(client, "variant", items["chair"].pk, 10)
    add(client, "package", items["package"].pk, 1)
    set_details(client, event_date, promo="spring")

    html = client.get(reverse("bookings:cart")).content.decode()

    quote = build_quote(
        [
            LineRequest(quantity=10, variant_id=items["chair"].pk),
            LineRequest(quantity=1, package_id=items["package"].pk),
        ],
        event_date,
        ZIP,
        "spring",
    )
    assert quote.is_bookable and quote.caution > 0 and quote.tax > 0
    for amount in (
        quote.items_subtotal,
        quote.package_subtotal,
        quote.discount,
        quote.delivery_fee,
        quote.setup_fee,
        quote.tax,
        quote.rental_total,
        quote.caution,
        quote.total,
        quote.deposit_amount,
    ):
        assert money(amount) in html, amount
    assert "Refundable caution deposit" in html
    assert "Discount (SPRING)" in html
    assert "Continue to booking" in html


def test_quote_errors_appear_next_to_what_they_refer_to(client, items, event_date):
    items["table"].stock_quantity = 1
    items["table"].save()
    add(client, "variant", items["chair"].pk, 2)
    add(client, "package", items["package"].pk, 1)
    set_details(client, event_date, zip_code="90210", promo="NOPE")

    html = client.get(reverse("bookings:cart")).content.decode()

    zip_block = html[html.index('id="cart-zip"') : html.index('id="cart-promo"')]
    assert "We do not deliver to ZIP code 90210 yet." in zip_block
    promo_block = html[html.index('id="cart-promo"') : html.index("Apply code")]
    assert "This promo code is not recognised." in promo_block
    package_line = html[html.index("Party set") :]
    assert f"Party set is not available on {event_date:%b %d, %Y}." in package_line[:1500]
    assert "Continue to booking" not in html
    assert "Fix the items marked above to continue." in html


def test_without_a_date_or_zip_the_cart_asks_for_them_and_shows_no_prices(client, items):
    add(client, "variant", items["chair"].pk, 2)

    html = client.get(reverse("bookings:cart")).content.decode()

    assert "Chair" in html
    assert "Choose your event date to see prices and availability." in html
    assert "Enter your delivery ZIP code to see delivery and setup fees." in html
    assert "Rental total" not in html and "Continue to booking" not in html


def test_lines_whose_rental_was_deleted_are_dropped(client, items, event_date):
    add(client, "variant", items["chair"].pk, 2)
    session = client.session
    session[cart_service.CART_KEY] = {**session[cart_service.CART_KEY], "variant:999999": 3}
    session.save()
    set_details(client, event_date)

    client.get(reverse("bookings:cart"))

    assert session_cart(client) == {f"variant:{items['chair'].pk}": 2}


def test_header_shows_cart_count_on_every_page(client, items):
    add(client, "variant", items["chair"].pk, 2)
    add(client, "variant", items["table"].pk, 1)

    html = client.get(reverse("core:about")).content.decode()

    assert "Booking cart, 2 items" in html
    assert html.count('id="cart-count"') == 1


def test_item_and_package_pages_have_add_to_booking_forms(client, items):
    item_html = client.get(items["chair"].item.get_absolute_url()).content.decode()
    package_html = client.get(items["package"].get_absolute_url()).content.decode()

    for html in (item_html, package_html):
        assert 'hx-post="/cart/add/"' in html and "to booking" in html
    assert f'name="id" value="{items["chair"].pk}"' in item_html
    assert f'name="id" value="{items["package"].pk}"' in package_html


def test_empty_cart_page_says_so(client):
    html = client.get(reverse("bookings:cart")).content.decode()

    assert "Your cart is empty." in html


def test_call_for_price_rentals_cannot_be_added(client, items):
    items["chair"].item.base_price = Decimal("0.00")
    items["chair"].item.save()
    items["package"].package_price = Decimal("0.00")
    items["package"].save()

    for kind, pk in (("variant", items["chair"].pk), ("package", items["package"].pk)):
        response = add(client, kind, pk)
        assert response.status_code == 422
        assert "for a price on this rental" in response.content.decode()
    assert session_cart(client) == {}
    item_html = client.get(items["chair"].item.get_absolute_url()).content.decode()
    package_html = client.get(items["package"].get_absolute_url()).content.decode()
    assert "to booking" not in item_html and "to booking" not in package_html
    assert "Ask for a price" in item_html and "Ask for a price" in package_html


def test_call_for_price_item_offers_a_prefilled_whatsapp_price_request(client, items, settings):
    settings.WHATSAPP_NUMBER = "14695550100"
    item = items["chair"].item
    item.base_price = Decimal("0.00")
    item.save()
    gold = ItemVariantFactory(item=item, name="Gold", stock_quantity=5)

    plain = client.get(item.get_absolute_url()).content.decode()
    picked = client.get(
        reverse("catalogue:availability", args=[item.slug]),
        {"variant": gold.pk, "date": "2027-06-12", "quantity": 1},
    ).content.decode()

    assert "Ask for a price" in plain and "Add to booking" not in plain
    assert "https://wa.me/14695550100?text=" in plain
    assert unescape_url(plain).count("price for the Chair?") == 1, "no option or date yet"
    assert "Ask for a price" in picked
    assert "price for the Chair (Gold) for my event on Saturday, June 12, 2027?" in (
        unescape_url(picked)
    )


def test_call_for_price_package_offers_a_price_request_with_the_chosen_date(
    client, items, event_date
):
    items["package"].package_price = Decimal("0.00")
    items["package"].save()
    set_details(client, event_date)

    html = client.get(items["package"].get_absolute_url()).content.decode()

    assert "Ask for a price" in html and "Add to booking" not in html
    expected = f"price for the Party set package for my event on {event_date:%A, %B} "
    assert expected in unescape_url(html)


def test_priced_rentals_do_not_show_ask_for_a_price(client, items):
    for url in (items["chair"].item.get_absolute_url(), items["package"].get_absolute_url()):
        html = client.get(url).content.decode()
        assert "Ask for a price" not in html and "to booking" in html


def unescape_url(html):
    from html import unescape
    from urllib.parse import unquote

    return unquote(unescape(html))


def test_cart_lines_show_the_item_photo_or_a_tinted_block(client, items, event_date):
    from apps.catalogue.models import ItemImage

    photo = ItemImage.objects.create(item=items["chair"].item, image="items/chair.jpg")
    ItemImage.objects.filter(pk=photo.pk).update(
        renditions_ready=True,
        renditions=[{"format": "jpeg", "width": 400, "height": 400, "name": "items/c-400.jpg"}],
    )
    add(client, "variant", items["chair"].pk, 2)
    add(client, "package", items["package"].pk, 1)
    set_details(client, event_date)

    html = client.get(reverse("bookings:cart")).content.decode()

    assert "items/c-400.jpg" in html
    assert 'data-placeholder="Party set"' in html
