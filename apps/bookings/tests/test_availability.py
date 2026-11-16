"""Tests for the availability engine."""

import threading
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.services import availability
from apps.bookings.services.availability import InsufficientStockError
from apps.bookings.tasks import release_expired_holds as release_expired_holds_task
from apps.bookings.tests.factories import (
    EVENT_DATE,
    BookingFactory,
    BookingItemFactory,
    ItemVariantFactory,
    StockAdjustmentFactory,
)

CUSTOMER = {
    "customer_name": "Ada Planner",
    "email": "ada@example.com",
    "phone": "555-0100",
    "delivery_address": "1 Main St, Dallas, TX",
}


def book(variant, quantity, status=Booking.Status.PAID, event_date=EVENT_DATE, **booking_kwargs):
    """Create a booking of one variant in the given status."""
    booking = BookingFactory(status=status, event_date=event_date, **booking_kwargs)
    BookingItemFactory(booking=booking, variant=variant, quantity=quantity)
    return booking


def in_minutes(minutes):
    return timezone.now() + timedelta(minutes=minutes)


@pytest.mark.django_db
class TestAvailableQuantity:
    def test_full_stock_when_nothing_is_booked(self):
        variant = ItemVariantFactory(stock_quantity=10)

        assert availability.available_quantity(variant, EVENT_DATE) == 10

    def test_counts_held_deposit_paid_paid_and_delivered(self):
        variant = ItemVariantFactory(stock_quantity=20)
        book(variant, 1, Booking.Status.HELD, hold_expires_at=in_minutes(10))
        book(variant, 2, Booking.Status.DEPOSIT_PAID)
        book(variant, 3, Booking.Status.PAID)
        book(variant, 4, Booking.Status.DELIVERED)

        assert availability.available_quantity(variant, EVENT_DATE) == 10

    @pytest.mark.parametrize(
        "status",
        [
            Booking.Status.ENQUIRY,
            Booking.Status.QUOTED,
            Booking.Status.RETURNED,
            Booking.Status.CANCELLED,
            Booking.Status.EXPIRED,
        ],
    )
    def test_ignores_statuses_that_do_not_hold_stock(self, status):
        variant = ItemVariantFactory(stock_quantity=10)
        book(variant, 4, status)

        assert availability.available_quantity(variant, EVENT_DATE) == 10

    def test_ignores_expired_hold_even_before_it_is_released(self):
        variant = ItemVariantFactory(stock_quantity=10)
        book(variant, 4, Booking.Status.HELD, hold_expires_at=in_minutes(-1))

        assert availability.available_quantity(variant, EVENT_DATE) == 10

    def test_held_booking_without_expiry_counts(self):
        variant = ItemVariantFactory(stock_quantity=10)
        book(variant, 4, Booking.Status.HELD, hold_expires_at=None)

        assert availability.available_quantity(variant, EVENT_DATE) == 6

    def test_only_counts_the_requested_date_and_variant(self):
        variant = ItemVariantFactory(stock_quantity=10)
        other_variant = ItemVariantFactory(item=variant.item, name="Black", stock_quantity=10)
        book(variant, 4, event_date=EVENT_DATE + timedelta(days=1))
        book(other_variant, 5)

        assert availability.available_quantity(variant, EVENT_DATE) == 10

    def test_never_negative_when_overcommitted(self):
        variant = ItemVariantFactory(stock_quantity=2)
        book(variant, 5)

        assert availability.available_quantity(variant, EVENT_DATE) == 0

    def test_damaged_and_under_repair_stock_is_excluded(self):
        variant = ItemVariantFactory(stock_quantity=10)
        StockAdjustmentFactory(variant=variant, reason="damaged", quantity=2)
        StockAdjustmentFactory(variant=variant, reason="under_repair", quantity=3)
        book(variant, 1)

        assert availability.available_quantity(variant, EVENT_DATE) == 4

    def test_resolved_adjustment_returns_stock(self):
        variant = ItemVariantFactory(stock_quantity=10)
        StockAdjustmentFactory(variant=variant, quantity=2, resolved_at=timezone.now())

        assert availability.available_quantity(variant, EVENT_DATE) == 10


@pytest.mark.django_db
class TestBufferDays:
    def test_default_buffer_is_zero(self, settings):
        assert settings.BOOKING_BUFFER_DAYS == 0
        variant = ItemVariantFactory(stock_quantity=1)
        book(variant, 1)

        assert availability.available_quantity(variant, EVENT_DATE) == 0
        assert availability.available_quantity(variant, EVENT_DATE + timedelta(days=1)) == 1
        assert availability.available_quantity(variant, EVENT_DATE - timedelta(days=1)) == 1

    def test_buffer_blocks_days_around_the_event(self, settings):
        """With a 1-day buffer a unit is tied up the day before and after its event.

        A new event needs its own day before and after too, so it must be at
        least 3 days away from the existing one.
        """
        settings.BOOKING_BUFFER_DAYS = 1
        variant = ItemVariantFactory(stock_quantity=1)
        book(variant, 1)

        for offset in (-2, -1, 0, 1, 2):
            day = EVENT_DATE + timedelta(days=offset)
            assert availability.available_quantity(variant, day) == 0, offset
        for offset in (-3, 3):
            day = EVENT_DATE + timedelta(days=offset)
            assert availability.available_quantity(variant, day) == 1, offset

    def test_bookings_that_do_not_overlap_each_other_share_units(self, settings):
        """Two bookings far enough apart reuse the same unit, so only one is tied up."""
        settings.BOOKING_BUFFER_DAYS = 1
        variant = ItemVariantFactory(stock_quantity=2)
        book(variant, 1, event_date=EVENT_DATE - timedelta(days=2))
        book(variant, 1, event_date=EVENT_DATE + timedelta(days=2))

        assert availability.available_quantity(variant, EVENT_DATE) == 1

    def test_reserve_respects_buffer(self, settings):
        settings.BOOKING_BUFFER_DAYS = 2
        variant = ItemVariantFactory(stock_quantity=1)
        book(variant, 1)

        with pytest.raises(InsufficientStockError):
            availability.reserve([(variant, 1)], EVENT_DATE + timedelta(days=2), **CUSTOMER)


@pytest.mark.django_db
class TestReserve:
    def test_creates_held_booking_with_expiry_and_price_snapshot(self, settings):
        settings.HOLD_MINUTES = 15
        default = ItemVariantFactory(stock_quantity=5, item__base_price=Decimal("25.00"))
        black = ItemVariantFactory(
            item=default.item, name="Black", stock_quantity=5, price_override=Decimal("30.00")
        )
        before = timezone.now()

        booking = availability.reserve([(default, 2), (black, 3)], EVENT_DATE, **CUSTOMER)

        assert booking.status == Booking.Status.HELD
        assert booking.event_date == EVENT_DATE
        assert booking.customer_name == "Ada Planner"
        expected = before + timedelta(minutes=15)
        assert expected <= booking.hold_expires_at <= expected + timedelta(seconds=5)
        items = {item.variant_id: item for item in booking.items.all()}
        assert items[default.pk].quantity == 2
        assert items[default.pk].unit_price == Decimal("25.00")
        assert items[black.pk].quantity == 3
        assert items[black.pk].unit_price == Decimal("30.00")
        assert availability.available_quantity(default, EVENT_DATE) == 3
        assert availability.available_quantity(black, EVENT_DATE) == 2

    def test_hold_minutes_setting_is_used(self, settings):
        settings.HOLD_MINUTES = 60
        variant = ItemVariantFactory(stock_quantity=1)

        booking = availability.reserve([(variant, 1)], EVENT_DATE, **CUSTOMER)

        assert booking.hold_expires_at > in_minutes(59)

    def test_shortage_raises_clear_error_and_creates_nothing(self):
        plenty = ItemVariantFactory(stock_quantity=10)
        short = ItemVariantFactory(stock_quantity=3, item__name="Gold Chiavari Chair")
        book(short, 2)

        with pytest.raises(InsufficientStockError) as raised:
            availability.reserve([(plenty, 1), (short, 2)], EVENT_DATE, **CUSTOMER)

        error = raised.value
        assert [(s.variant, s.requested, s.available) for s in error.shortages] == [(short, 2, 1)]
        assert "Gold Chiavari Chair (Default): requested 2, only 1 available" in str(error)
        assert Booking.objects.count() == 1
        assert availability.available_quantity(plenty, EVENT_DATE) == 10

    def test_duplicate_lines_for_a_variant_are_added_together(self):
        variant = ItemVariantFactory(stock_quantity=3)

        with pytest.raises(InsufficientStockError):
            availability.reserve([(variant, 2), (variant, 2)], EVENT_DATE, **CUSTOMER)

    def test_damaged_stock_cannot_be_reserved(self):
        variant = ItemVariantFactory(stock_quantity=2)
        StockAdjustmentFactory(variant=variant, quantity=2)

        with pytest.raises(InsufficientStockError):
            availability.reserve([(variant, 1)], EVENT_DATE, **CUSTOMER)

    @pytest.mark.parametrize("items", [[], "zero"])
    def test_rejects_empty_or_zero_quantity_requests(self, items):
        variant = ItemVariantFactory()
        lines = [(variant, 0)] if items == "zero" else []

        with pytest.raises(ValueError):
            availability.reserve(lines, EVENT_DATE, **CUSTOMER)


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_reservations_for_the_last_unit_only_one_succeeds():
    variant = ItemVariantFactory(stock_quantity=1)
    barrier = threading.Barrier(2)
    results = []

    def attempt():
        try:
            barrier.wait(timeout=10)
            availability.reserve([(variant, 1)], EVENT_DATE, **CUSTOMER)
            results.append("reserved")
        except InsufficientStockError:
            results.append("short")
        except Exception as error:  # noqa: BLE001
            results.append(repr(error))
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(results) == ["reserved", "short"]
    assert Booking.objects.filter(status=Booking.Status.HELD).count() == 1
    assert availability.available_quantity(variant, EVENT_DATE) == 0


@pytest.mark.django_db(transaction=True)
def test_many_simultaneous_reservations_never_oversell():
    variant = ItemVariantFactory(stock_quantity=3)
    other = ItemVariantFactory(stock_quantity=3)
    barrier = threading.Barrier(8)
    results = []

    def attempt(lines):
        try:
            barrier.wait(timeout=10)
            availability.reserve(lines, EVENT_DATE, **CUSTOMER)
            results.append("reserved")
        except InsufficientStockError:
            results.append("short")
        except Exception as error:  # noqa: BLE001
            results.append(repr(error))
        finally:
            connection.close()

    orders = [[(variant, 1), (other, 1)], [(other, 1), (variant, 1)]] * 4
    threads = [threading.Thread(target=attempt, args=(lines,)) for lines in orders]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(results) == ["reserved"] * 3 + ["short"] * 5
    assert availability.available_quantity(variant, EVENT_DATE) == 0
    assert availability.available_quantity(other, EVENT_DATE) == 0


@pytest.mark.django_db
class TestReleaseExpiredHolds:
    def test_expired_holds_release_stock(self):
        variant = ItemVariantFactory(stock_quantity=1)
        booking = availability.reserve([(variant, 1)], EVENT_DATE, **CUSTOMER)
        assert availability.available_quantity(variant, EVENT_DATE) == 0
        Booking.objects.filter(pk=booking.pk).update(hold_expires_at=in_minutes(-1))

        released = availability.release_expired_holds()

        booking.refresh_from_db()
        assert released == 1
        assert booking.status == Booking.Status.EXPIRED
        assert availability.available_quantity(variant, EVENT_DATE) == 1
        assert availability.reserve([(variant, 1)], EVENT_DATE, **CUSTOMER).pk != booking.pk

    def test_leaves_live_holds_and_other_statuses_alone(self):
        variant = ItemVariantFactory(stock_quantity=10)
        live = book(variant, 1, Booking.Status.HELD, hold_expires_at=in_minutes(5))
        open_ended = book(variant, 1, Booking.Status.HELD, hold_expires_at=None)
        paid = book(variant, 1, Booking.Status.PAID, hold_expires_at=in_minutes(-30))

        assert availability.release_expired_holds() == 0

        for booking, status in [(live, "held"), (open_ended, "held"), (paid, "paid")]:
            booking.refresh_from_db()
            assert booking.status == status

    def test_celery_task_releases_holds_and_is_scheduled_every_minute(self, settings):
        variant = ItemVariantFactory(stock_quantity=1)
        book(variant, 1, Booking.Status.HELD, hold_expires_at=in_minutes(-1))

        assert release_expired_holds_task() == 1
        assert availability.available_quantity(variant, EVENT_DATE) == 1
        schedule = settings.CELERY_BEAT_SCHEDULE["release-expired-holds"]
        assert schedule == {"task": release_expired_holds_task.name, "schedule": 60.0}
