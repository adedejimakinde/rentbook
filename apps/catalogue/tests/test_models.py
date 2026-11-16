"""Catalogue model behaviour and the one-variant-minimum rule in the admin."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.bookings.tests.factories import CategoryFactory, ItemVariantFactory
from apps.catalogue.models import Item

pytestmark = pytest.mark.django_db


def test_unit_price_falls_back_to_base_price():
    default = ItemVariantFactory(item__base_price=Decimal("25.00"))
    black = ItemVariantFactory(item=default.item, name="Black", price_override=Decimal("30.00"))
    free = ItemVariantFactory(item=default.item, name="Promo", price_override=Decimal("0.00"))

    assert default.unit_price == Decimal("25.00")
    assert black.unit_price == Decimal("30.00")
    assert free.unit_price == Decimal("0.00")


def item_form(category, variants):
    data = {
        "category": category.pk,
        "name": "Folding Chair",
        "slug": "folding-chair",
        "description": "",
        "base_price": "2.50",
        "is_active": "on",
        "variants-TOTAL_FORMS": len(variants),
        "variants-INITIAL_FORMS": 0,
        "variants-MIN_NUM_FORMS": 1,
        "variants-MAX_NUM_FORMS": 1000,
    }
    for index, (name, stock) in enumerate(variants):
        data[f"variants-{index}-name"] = name
        data[f"variants-{index}-stock_quantity"] = stock
        data[f"variants-{index}-price_override"] = ""
    return data


def test_admin_rejects_an_item_without_variants(admin_client):
    response = admin_client.post(
        reverse("admin:catalogue_item_add"), item_form(CategoryFactory(), [])
    )

    assert response.status_code == 200
    assert Item.objects.count() == 0


def test_admin_saves_an_item_with_a_variant(admin_client):
    response = admin_client.post(
        reverse("admin:catalogue_item_add"), item_form(CategoryFactory(), [("Default", 100)])
    )

    assert response.status_code == 302
    assert Item.objects.get().variants.get().stock_quantity == 100
