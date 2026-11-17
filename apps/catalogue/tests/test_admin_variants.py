"""Variant edits on the item admin are validated and deleted under the variant row locks."""

from unittest import mock

import pytest
from django.urls import reverse

from apps.bookings.models import Booking
from apps.bookings.tests.factories import (
    BookingItemFactory,
    ItemVariantFactory,
    StockAdjustmentFactory,
)
from apps.catalogue.models import ItemVariant
from apps.inventory import services as inventory

pytestmark = pytest.mark.django_db


def item_form(item, rows):
    """POST data for the item admin with its variant rows (dicts of variant, stock, DELETE)."""
    data = {
        "category": item.category_id,
        "name": item.name,
        "slug": item.slug,
        "description": "",
        "base_price": "25.00",
        "caution_fee": "0.00",
        "is_active": "on",
        "variants-TOTAL_FORMS": len(rows),
        "variants-INITIAL_FORMS": len(rows),
        "variants-MIN_NUM_FORMS": 1,
        "variants-MAX_NUM_FORMS": 1000,
        "images-TOTAL_FORMS": 0,
        "images-INITIAL_FORMS": 0,
        "images-MIN_NUM_FORMS": 0,
        "images-MAX_NUM_FORMS": 1000,
    }
    for index, row in enumerate(rows):
        variant = row["variant"]
        data[f"variants-{index}-id"] = variant.pk
        data[f"variants-{index}-item"] = item.pk
        data[f"variants-{index}-name"] = variant.name
        data[f"variants-{index}-price_override"] = ""
        data[f"variants-{index}-stock_quantity"] = row.get("stock", variant.stock_quantity)
        if row.get("DELETE"):
            data[f"variants-{index}-DELETE"] = "on"
    return data


def change_url(item):
    return reverse("admin:catalogue_item_change", args=[item.pk])


def while_locking(action):
    """Run action the first time variant rows are locked: a change that committed just before."""
    real = inventory._lock_variants
    done = []

    def lock(variant_ids):
        if not done:
            done.append(True)
            action()
        return real(variant_ids)

    return mock.patch.object(inventory, "_lock_variants", side_effect=lock)


def errors_in(response):
    formset = response.context["inline_admin_formsets"][0].formset
    return " ".join(formset.non_form_errors())


def test_a_stock_cut_made_invalid_under_the_lock_is_a_form_error(admin_client):
    variant = ItemVariantFactory(stock_quantity=5)
    item = variant.item

    with while_locking(lambda: StockAdjustmentFactory(variant=variant, quantity=4)):
        response = admin_client.post(
            change_url(item), item_form(item, [{"variant": variant, "stock": 3}])
        )

    assert response.status_code == 200
    assert "damaged or under repair" in errors_in(response)
    variant.refresh_from_db()
    assert variant.stock_quantity == 5


def test_a_variant_booked_just_before_its_delete_is_a_form_error(admin_client):
    keep = ItemVariantFactory(stock_quantity=5)
    item = keep.item
    doomed = ItemVariantFactory(item=item, name="Gold", stock_quantity=2)

    with while_locking(
        lambda: BookingItemFactory(variant=doomed, booking__status=Booking.Status.PAID)
    ):
        response = admin_client.post(
            change_url(item),
            item_form(item, [{"variant": keep}, {"variant": doomed, "DELETE": True}]),
        )

    assert response.status_code == 200
    assert "on bookings" in errors_in(response)
    assert ItemVariant.objects.filter(pk=doomed.pk).exists()


def test_deleted_variants_go_through_the_locked_service(admin_client):
    keep = ItemVariantFactory(stock_quantity=5)
    item = keep.item
    doomed = ItemVariantFactory(item=item, name="Gold", stock_quantity=2)

    with mock.patch.object(inventory, "save_variants", wraps=inventory.save_variants) as saved:
        response = admin_client.post(
            change_url(item),
            item_form(item, [{"variant": keep}, {"variant": doomed, "DELETE": True}]),
        )

    assert response.status_code == 302
    assert not ItemVariant.objects.filter(pk=doomed.pk).exists()
    assert [variant.name for variant in saved.call_args.kwargs["deleted"]] == ["Gold"]
