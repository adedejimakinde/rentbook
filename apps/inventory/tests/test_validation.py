"""Validation of stock and adjustment quantities."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.bookings.models import BookingItem
from apps.bookings.tests.factories import (
    BookingFactory,
    BookingItemFactory,
    ItemVariantFactory,
    StockAdjustmentFactory,
)
from apps.inventory.models import StockAdjustment

pytestmark = pytest.mark.django_db


def test_stock_quantity_cannot_be_negative():
    variant = ItemVariantFactory.build(item=ItemVariantFactory().item, name="Black")
    variant.stock_quantity = -1

    with pytest.raises(ValidationError) as raised:
        variant.full_clean()
    assert "stock_quantity" in raised.value.message_dict
    with pytest.raises(IntegrityError), transaction.atomic():
        variant.save()


def test_adjustment_quantity_cannot_be_negative():
    adjustment = StockAdjustment(variant=ItemVariantFactory(), reason="damaged", quantity=-1)

    with pytest.raises(ValidationError) as raised:
        adjustment.full_clean()
    assert "quantity" in raised.value.message_dict


def test_unresolved_adjustments_cannot_exceed_stock():
    variant = ItemVariantFactory(stock_quantity=5)
    StockAdjustmentFactory(variant=variant, quantity=3)
    StockAdjustmentFactory(variant=variant, quantity=4, resolved_at=timezone.now())

    StockAdjustment(variant=variant, reason="under_repair", quantity=2).full_clean()
    with pytest.raises(ValidationError) as raised:
        StockAdjustment(variant=variant, reason="under_repair", quantity=3).full_clean()
    assert "Only 2 of 5" in raised.value.message_dict["quantity"][0]


def test_editing_an_adjustment_does_not_count_itself_twice():
    variant = ItemVariantFactory(stock_quantity=5)
    adjustment = StockAdjustmentFactory(variant=variant, quantity=5)

    adjustment.full_clean()
    adjustment.quantity = 6
    with pytest.raises(ValidationError):
        adjustment.full_clean()


def test_stock_cannot_drop_below_out_of_service_units():
    variant = ItemVariantFactory(stock_quantity=5)
    StockAdjustmentFactory(variant=variant, quantity=3)

    variant.stock_quantity = 3
    variant.full_clean()
    variant.stock_quantity = 2
    with pytest.raises(ValidationError) as raised:
        variant.full_clean()
    assert "stock_quantity" in raised.value.message_dict


def test_booking_item_quantity_must_be_at_least_one():
    booking, variant = BookingFactory(), ItemVariantFactory()
    item = BookingItemFactory.build(booking=booking, variant=variant, quantity=0)

    with pytest.raises(ValidationError) as raised:
        item.full_clean()
    assert "quantity" in raised.value.message_dict
    with pytest.raises(IntegrityError), transaction.atomic():
        item.save()
    assert BookingItem.objects.count() == 0
