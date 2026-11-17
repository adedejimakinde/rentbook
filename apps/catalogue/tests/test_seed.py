"""The seed command creates the client's catalogue once and never overwrites edits."""

from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from apps.catalogue.models import Category, Item, ItemVariant

pytestmark = pytest.mark.django_db


def seed():
    out = StringIO()
    call_command("seed_catalogue", stdout=out)
    return out.getvalue()


def counts():
    return Category.objects.count(), Item.objects.count(), ItemVariant.objects.count()


def test_seed_creates_the_real_item_list():
    seed()

    assert counts() == (8, 19, 22)
    names = set(Item.objects.values_list("name", flat=True))
    assert {"Bounce house", "Chiavari chair", "Golf simulator", "Generator"} <= names
    assert Item.objects.get(name="Canopy").category.name == "Tents and canopies"
    assert Item.objects.get(name="Slushy machine").category.name == "Concessions"


def test_seed_variants():
    seed()

    def variants(name):
        return set(ItemVariant.objects.filter(item__name=name).values_list("name", flat=True))

    assert variants("Garden chair") == {"Black", "White"}
    assert variants("Chiavari chair") == {"Black", "Silver", "Gold"}
    assert variants("Popcorn machine") == {"Default"}


def test_seed_prices_are_zero_and_stock_is_unconfirmed():
    seed()

    assert set(Item.objects.values_list("base_price", flat=True)) == {Decimal("0.00")}
    assert not ItemVariant.objects.filter(stock_confirmed=True).exists()
    assert set(ItemVariant.objects.values_list("stock_quantity", flat=True)) == {0}
    assert not Item.objects.filter(images__isnull=False).exists()


def test_seed_is_idempotent():
    seed()
    first = counts()

    output = seed()

    assert counts() == first
    assert "Created 0 categories, 0 items and 0 variants" in output


def test_seed_never_overwrites_staff_edits():
    seed()
    item = Item.objects.get(name="Bounce house")
    item.base_price = Decimal("150.00")
    item.short_description = "Edited by staff"
    item.save()
    variant = item.variants.get()
    variant.stock_quantity = 4
    variant.stock_confirmed = True
    variant.save()

    seed()

    item.refresh_from_db()
    variant.refresh_from_db()
    assert item.base_price == Decimal("150.00")
    assert item.short_description == "Edited by staff"
    assert (variant.stock_quantity, variant.stock_confirmed) == (4, True)
