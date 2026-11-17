"""Taking units out of service or cutting stock is checked against bookings already made."""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.services import availability
from apps.bookings.tests.factories import (
    EVENT_DATE,
    BookingFactory,
    BookingItemFactory,
    ItemVariantFactory,
)
from apps.inventory import services
from apps.inventory.models import StockAdjustment

pytestmark = pytest.mark.django_db

Status = Booking.Status


def book(variant, quantity, status=Status.PAID, event_date=EVENT_DATE, **kwargs):
    if status == Status.HELD:
        kwargs.setdefault("hold_expires_at", timezone.now() + timedelta(hours=1))
    booking = BookingFactory(status=status, event_date=event_date, **kwargs)
    BookingItemFactory(booking=booking, variant=variant, quantity=quantity)
    return booking


def flagged(*bookings):
    return [Booking.objects.get(pk=booking.pk).needs_attention for booking in bookings]


def damage(variant, quantity):
    return StockAdjustment(
        variant=variant, reason=StockAdjustment.Reason.DAMAGED, quantity=quantity
    )


def test_damage_with_room_to_spare_leaves_bookings_alone(django_capture_on_commit_callbacks):
    variant = ItemVariantFactory(stock_quantity=5)
    paid = book(variant, 2)

    with django_capture_on_commit_callbacks(execute=True):
        short = services.save_adjustment(damage(variant, 3))

    assert short == []
    assert StockAdjustment.objects.get().quantity == 3
    assert flagged(paid) == [False]


def test_damage_flags_the_newest_booking_left_short(django_capture_on_commit_callbacks):
    variant = ItemVariantFactory(stock_quantity=3)
    first = book(variant, 2)
    second = book(variant, 1, status=Status.HELD)

    with django_capture_on_commit_callbacks(execute=True):
        short = services.save_adjustment(damage(variant, 1))

    assert short == [second]
    assert flagged(first, second) == [False, True]
    assert StockAdjustment.objects.count() == 1


def test_only_future_stock_holding_bookings_count(django_capture_on_commit_callbacks):
    variant = ItemVariantFactory(stock_quantity=2)
    yesterday = timezone.localdate() - timedelta(days=1)
    past = book(variant, 2, status=Status.DELIVERED, event_date=yesterday)
    lapsed = book(variant, 2, status=Status.HELD, hold_expires_at=timezone.now())
    cancelled = book(variant, 2, status=Status.CANCELLED)
    enquiry = book(variant, 2, status=Status.ENQUIRY)
    returned = book(variant, 2, status=Status.RETURNED)

    with django_capture_on_commit_callbacks(execute=True):
        short = services.save_adjustment(damage(variant, 2))

    assert short == []
    assert not any(flagged(past, lapsed, cancelled, enquiry, returned))


def test_bookings_on_different_days_only_clash_within_the_buffer(
    settings, django_capture_on_commit_callbacks
):
    settings.BOOKING_BUFFER_DAYS = 1
    variant = ItemVariantFactory(stock_quantity=2)
    monday = book(variant, 1, event_date=EVENT_DATE)
    tuesday = book(variant, 1, event_date=EVENT_DATE + timedelta(days=1))
    friday = book(variant, 1, event_date=EVENT_DATE + timedelta(days=4))

    with django_capture_on_commit_callbacks(execute=True):
        short = services.save_adjustment(damage(variant, 1))

    assert short == [tuesday]
    assert flagged(monday, tuesday, friday) == [False, True, False]


def test_cutting_stock_flags_bookings_left_short(django_capture_on_commit_callbacks):
    variant = ItemVariantFactory(stock_quantity=4)
    first = book(variant, 2)
    second = book(variant, 2)

    variant.stock_quantity = 3
    with django_capture_on_commit_callbacks(execute=True):
        short = services.save_variants([variant])

    variant.refresh_from_db()
    assert variant.stock_quantity == 3
    assert short == [second]
    assert flagged(first, second) == [False, True]


def test_adjustment_is_validated_again_under_the_lock():
    variant = ItemVariantFactory(stock_quantity=3)
    StockAdjustment.objects.create(
        variant=variant, reason=StockAdjustment.Reason.UNDER_REPAIR, quantity=2
    )

    with pytest.raises(ValidationError):
        services.save_adjustment(damage(variant, 2))
    assert StockAdjustment.objects.count() == 1


def test_variant_row_is_locked_and_no_booking_is_touched_until_commit(
    django_capture_on_commit_callbacks,
):
    """The variant is locked for the change; bookings are flagged only after it commits.

    Locking or updating a booking while holding a variant lock would take the
    locks in the opposite order to the ledger (booking, then stock).
    """
    variant = ItemVariantFactory(stock_quantity=1)
    booking = book(variant, 1)
    statements = []

    def capture(execute, sql, params, many, context):
        statements.append(sql)
        return execute(sql, params, many, context)

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        with connection.execute_wrapper(capture):
            services.save_adjustment(damage(variant, 1))

    locks = [sql for sql in statements if "FOR UPDATE" in sql]
    assert any('"catalogue_itemvariant"' in sql.split("FROM")[1][:40] for sql in locks)
    assert not any('UPDATE "bookings_booking"' in sql for sql in statements)
    assert not any('"bookings_booking"' in sql.split("FROM")[1][:40] for sql in locks)
    assert flagged(booking) == [False]

    for callback in callbacks:
        callback()
    assert flagged(booking) == [True]


def test_bookings_left_short_reports_every_short_booking():
    variant = ItemVariantFactory(stock_quantity=2)
    book(variant, 2)
    later = book(variant, 1)

    assert availability.bookings_left_short([variant.pk]) == [later]


def test_admin_stock_adjustment_warns_and_flags(admin_client, django_capture_on_commit_callbacks):
    variant = ItemVariantFactory(stock_quantity=1, item__name="Gold Chiavari Chair")
    booking = book(variant, 1)

    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client.post(
            reverse("admin:inventory_stockadjustment_add"),
            {
                "variant": variant.pk,
                "reason": "damaged",
                "quantity": 1,
                "note": "",
                "resolved_at_0": "",
                "resolved_at_1": "",
            },
            follow=True,
        )

    assert response.status_code == 200
    assert StockAdjustment.objects.count() == 1
    text = " ".join(str(message) for message in response.context["messages"])
    assert f"#{booking.pk}" in text
    assert "short" in text
    assert flagged(booking) == [True]


def test_admin_item_stock_cut_warns_and_flags(admin_client, django_capture_on_commit_callbacks):
    variant = ItemVariantFactory(stock_quantity=2)
    item = variant.item
    booking = book(variant, 2)

    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client.post(
            reverse("admin:catalogue_item_change", args=[item.pk]),
            {
                "category": item.category_id,
                "name": item.name,
                "slug": item.slug,
                "description": "",
                "base_price": "25.00",
                "caution_fee": "0.00",
                "is_active": "on",
                "variants-TOTAL_FORMS": 1,
                "variants-INITIAL_FORMS": 1,
                "variants-MIN_NUM_FORMS": 1,
                "variants-MAX_NUM_FORMS": 1000,
                "images-TOTAL_FORMS": 0,
                "images-INITIAL_FORMS": 0,
                "images-MIN_NUM_FORMS": 0,
                "images-MAX_NUM_FORMS": 1000,
                "variants-0-id": variant.pk,
                "variants-0-item": item.pk,
                "variants-0-name": "Default",
                "variants-0-price_override": "",
                "variants-0-stock_quantity": 1,
            },
            follow=True,
        )

    assert response.status_code == 200
    variant.refresh_from_db()
    assert variant.stock_quantity == 1
    text = " ".join(str(message) for message in response.context["messages"])
    assert f"#{booking.pk}" in text
    assert flagged(booking) == [True]
