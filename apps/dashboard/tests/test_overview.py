"""Dashboard cards: each shows the right bookings, and "today" follows the business time zone."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.tests.factories import DeliveryZoneFactory
from apps.bookings.tests.helpers import add_payment
from apps.dashboard.services import overview, stops
from apps.dashboard.tests.conftest import make_booking
from apps.payments.models import Payment

pytestmark = pytest.mark.django_db
D = Decimal
S = Booking.Status


def noon(day):
    """12:00 local on day, as an aware datetime."""
    return timezone.make_aware(datetime.combine(day, datetime.min.time().replace(hour=12)))


def test_received_this_month_counts_rental_money_net_of_refunds(today):
    now = noon(today)
    booking = make_booking(S.PAID, today + timedelta(days=10), total="300.00", paid="0.00")
    add_payment(booking, "300.00", kind=Payment.Kind.FULL, received_at=now, refunded_amount="50.00")
    add_payment(booking, "0.00", caution="75.00", received_at=now)
    add_payment(booking, "999.00", received_at=now, status=Payment.Status.FAILED)
    assert overview.build(now).received_month == D("250.00")


def test_received_this_month_leaves_out_other_months(today):
    now = noon(today)
    last_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    booking = make_booking(S.PAID, today + timedelta(days=10), total="100.00", paid="0.00")
    add_payment(booking, "100.00", received_at=noon(last_month))
    assert overview.build(now).received_month == D("0.00")


def test_still_owed_sums_balances_of_upcoming_confirmed_bookings_only(today):
    make_booking(S.DEPOSIT_PAID, today + timedelta(days=5), total="400.00", paid="100.00")
    make_booking(S.PAID, today + timedelta(days=5), total="200.00", paid="200.00")
    make_booking(
        S.HELD,
        today + timedelta(days=5),
        total="900.00",
        paid="0.00",
        hold_expires_at=timezone.now() + timedelta(hours=5),
    )
    make_booking(S.DEPOSIT_PAID, today - timedelta(days=3), total="500.00", paid="100.00")
    make_booking(S.CANCELLED, today + timedelta(days=5), total="700.00", paid="0.00")
    assert overview.build(noon(today)).still_owed == D("300.00")


def test_caution_held_is_collected_less_released(today):
    held = make_booking(
        S.PAID, today + timedelta(days=5), caution_fee="100.00", caution_paid="100.00"
    )
    returned = make_booking(
        S.RETURNED, today - timedelta(days=5), caution_fee="60.00", caution_paid="60.00"
    )
    add_payment(returned, "60.00", kind=Payment.Kind.CAUTION_REFUND)
    assert held.caution_held == D("100.00")
    assert overview.build(noon(today)).caution_held == D("100.00")


def test_bookings_this_week_counts_monday_to_sunday_and_skips_dead_ones(today):
    week_start = today - timedelta(days=today.weekday())
    make_booking(S.PAID, week_start)
    make_booking(S.DEPOSIT_PAID, week_start + timedelta(days=6))
    make_booking(S.RETURNED, week_start + timedelta(days=2))
    make_booking(S.PAID, week_start + timedelta(days=7))
    make_booking(S.PAID, week_start - timedelta(days=1))
    make_booking(S.CANCELLED, week_start + timedelta(days=1))
    make_booking(
        S.HELD, week_start + timedelta(days=1), hold_expires_at=timezone.now() - timedelta(hours=1)
    )
    assert overview.build(noon(today)).week_count == 3


def test_todays_jobs_have_time_kind_customer_city_and_tags(today):
    DeliveryZoneFactory(name="Collin", zip_codes=["75034"])
    delivery = make_booking(
        S.DEPOSIT_PAID,
        today,
        total="240.00",
        paid="0.00",
        customer_name="Grace Fellowship",
        delivery_address="1 Elm St\nGarland, TX 75040",
        city="Garland",
        pickup_date=today + timedelta(days=1),
        delivery_time=datetime(2000, 1, 1, 10, 0).time(),
    )
    pickup = make_booking(
        S.DELIVERED,
        today - timedelta(days=1),
        total="100.00",
        caution_fee="50.00",
        caution_paid="50.00",
        customer_name="Okafor wedding",
        delivery_address="9 Oak St",
        zip_code="75034",
        pickup_date=today,
        pickup_time=datetime(2000, 1, 1, 14, 0).time(),
    )
    data = overview.build(noon(today))
    assert [(job.stop.booking.pk, job.stop.kind) for job in data.jobs] == [
        (delivery.pk, stops.DELIVERY),
        (pickup.pk, stops.PICKUP),
    ]
    first, second = data.jobs
    assert (first.stop.time_label, first.city) == ("10:00 AM", "Garland")
    assert ("Collect $240", "danger") in first.tags
    assert (second.stop.time_label, second.city) == ("2:00 PM", "Collin")
    assert ("Inspect", "warning") in second.tags
    assert data.summary == "1 delivery and 1 pickup today. $240 to collect on site."


def test_a_paid_job_is_tagged_paid_and_an_empty_day_says_so(today):
    make_booking(
        S.PAID,
        today,
        total="100.00",
        pickup_date=today + timedelta(days=1),
        delivery_time=datetime(2000, 1, 1, 9, 0).time(),
    )
    (job,) = overview.build(noon(today)).jobs
    assert ("Paid", "success") in job.tags
    assert overview.build(noon(today + timedelta(days=30))).summary == (
        "No deliveries or pickups today."
    )


def test_needs_attention_is_oldest_first_with_reasons(today):
    refund = make_booking(S.RETURNED, today - timedelta(days=9), needs_attention=True)
    add_payment(refund, "20.00", kind=Payment.Kind.RENTAL_REFUND, status=Payment.Status.FAILED)
    tax = make_booking(
        S.PAID, today + timedelta(days=9), needs_attention=True, tax_exempt_requested=True
    )
    other = make_booking(S.PAID, today + timedelta(days=9), needs_attention=True)
    make_booking(S.PAID, today + timedelta(days=9))
    Booking.objects.filter(pk=refund.pk).update(updated_at=timezone.now() - timedelta(days=3))
    Booking.objects.filter(pk=tax.pk).update(updated_at=timezone.now() - timedelta(days=2))
    Booking.objects.filter(pk=other.pk).update(updated_at=timezone.now() - timedelta(days=1))
    rows = overview.build(noon(today)).attention
    assert [row.booking.pk for row in rows] == [refund.pk, tax.pk, other.pk]
    assert [row.reason for row in rows] == [
        "Refund failed at Stripe. Check and resend.",
        "Tax exemption requested. Verify certificate.",
        "Needs a look.",
    ]


def test_holds_expiring_within_24_hours_only(today):
    now = noon(today)
    soon = make_booking(S.HELD, today + timedelta(days=9), hold_expires_at=now + timedelta(hours=3))
    make_booking(S.HELD, today + timedelta(days=9), hold_expires_at=now + timedelta(hours=30))
    make_booking(S.HELD, today + timedelta(days=9), hold_expires_at=now - timedelta(hours=1))
    assert [booking.pk for booking in overview.build(now).expiring_holds] == [soon.pk]


def test_stock_warnings_list_bookings_left_short_by_damage(today):
    from apps.bookings.tests.factories import BookingItemFactory, StockAdjustmentFactory

    booking = make_booking(S.PAID, today + timedelta(days=9))
    line = booking.items.get()
    line.variant.stock_quantity = 1
    line.variant.save()
    ok = make_booking(S.PAID, today + timedelta(days=40))
    assert ok.pk
    StockAdjustmentFactory(variant=line.variant, quantity=1)
    assert BookingItemFactory  # keeps the import used for readers of the factories
    assert [b.pk for b in overview.build(noon(today)).stock_short] == [booking.pk]


def test_next_seven_days_lists_status_and_balance_without_today(today):
    make_booking(S.PAID, today, total="100.00")
    deposit = make_booking(S.DEPOSIT_PAID, today + timedelta(days=2), total="410.00", paid="100.00")
    make_booking(S.PAID, today + timedelta(days=8))
    rows = overview.build(noon(today)).upcoming
    assert [row.booking.pk for row in rows] == [deposit.pk]
    assert (rows[0].label, rows[0].tone) == ("Deposit paid", "brand")
    assert rows[0].booking.m_balance == D("310.00")


def test_today_rolls_over_at_local_midnight_not_utc():
    """June 12, 04:30 UTC is still June 11 at 23:30 in Dallas; 05:30 UTC is June 12."""
    june_11 = datetime(2027, 6, 11).date()
    june_12 = june_11 + timedelta(days=1)
    on_11 = make_booking(S.PAID, june_11, pickup_date=june_12)
    on_12 = make_booking(S.PAID, june_12, pickup_date=june_12 + timedelta(days=1))
    late = datetime(2027, 6, 12, 4, 30, tzinfo=UTC)
    early = datetime(2027, 6, 12, 5, 30, tzinfo=UTC)
    assert overview.build(late).today == june_11
    assert [job.stop.booking.pk for job in overview.build(late).jobs] == [on_11.pk]
    assert overview.build(early).today == june_12
    assert [(job.stop.booking.pk, job.stop.kind) for job in overview.build(early).jobs] == [
        (on_11.pk, stops.PICKUP),
        (on_12.pk, stops.DELIVERY),
    ]


def test_month_total_rolls_over_at_local_midnight():
    booking = make_booking(S.PAID, datetime(2027, 7, 20).date(), total="100.00", paid="0.00")
    add_payment(booking, "100.00", received_at=datetime(2027, 7, 1, 4, 30, tzinfo=UTC))
    assert overview.build(datetime(2027, 6, 20, 17, 0, tzinfo=UTC)).received_month == D("100.00")
    assert overview.build(datetime(2027, 7, 20, 17, 0, tzinfo=UTC)).received_month == D("0.00")


def test_greeting_follows_the_local_hour():
    assert overview.greeting(datetime(2027, 6, 12, 14, 0, tzinfo=UTC)) == "Good morning"
    assert overview.greeting(datetime(2027, 6, 12, 19, 0, tzinfo=UTC)) == "Good afternoon"
    assert overview.greeting(datetime(2027, 6, 13, 1, 0, tzinfo=UTC)) == "Good evening"
