"""Items that need a power outlet say so on their page and are editable in the admin."""

import pytest
from django.urls import reverse

from apps.bookings.tests.factories import ItemFactory, ItemVariantFactory

pytestmark = pytest.mark.django_db


def test_item_page_shows_needs_a_power_outlet(client):
    item = ItemVariantFactory(item=ItemFactory(name="Popcorn machine", needs_power=True)).item
    html = client.get(item.get_absolute_url()).content.decode()
    assert "Needs a power outlet" in html


def test_item_page_says_nothing_about_power_otherwise(client):
    item = ItemVariantFactory(item=ItemFactory(name="Chair", needs_power=False)).item
    assert "Needs a power outlet" not in client.get(item.get_absolute_url()).content.decode()


def test_needs_power_is_on_the_admin_form(admin_client):
    item = ItemVariantFactory(item=ItemFactory(name="Slushy machine")).item
    html = admin_client.get(reverse("admin:catalogue_item_change", args=[item.pk])).content.decode()
    assert 'name="needs_power"' in html
