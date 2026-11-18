"""Query budgets with 200 bookings: the dashboard and crew schedule each stay under 15."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.tests.factories import (
    BookingItemFactory,
    DeliveryZoneFactory,
    ItemVariantFactory,
    StockAdjustmentFactory,
)
from apps.bookings.tests.helpers import add_payment
from apps.core import site_settings
from apps.dashboard.tests.conftest import staff
from apps.payments.models import Payment

pytestmark = pytest.mark.django_db
S = Booking.Status
BUDGET = 14


@pytest.fixture
def busy(db):
    """200 bookings around today: stops today, a week of upcoming events, flags and payments."""
    today = timezone.localdate()
    statuses = [S.PAID, S.DEPOSIT_PAID, S.DELIVERED, S.RETURNED, S.HELD, S.CANCELLED]
    variants = [ItemVariantFactory(stock_quantity=500) for _ in range(5)]
    for number in range(200):
        status = statuses[number % len(statuses)]
        booking = Booking.objects.create(
            customer_name=f"Customer {number}",
            email=f"c{number}@example.com",
            phone=f"469-555-{number:04d}",
            event_date=today + timedelta(days=(number % 14) - 3),
            delivery_address=f"{number} Main St\nPlano, TX 75024",
            status=status,
            hold_expires_at=timezone.now() + timedelta(hours=number % 40)
            if status == S.HELD
            else None,
            needs_attention=number % 17 == 0,
            caution_fee=Decimal("25.00"),
            pickup_date=today + timedelta(days=(number % 14) - 2) if number % 3 else None,
        )
        for variant in variants[: 1 + number % 3]:
            BookingItemFactory(booking=booking, variant=variant, quantity=1 + number % 4)
        if status in (S.PAID, S.DELIVERED, S.RETURNED, S.DEPOSIT_PAID):
            add_payment(
                booking,
                "20.00",
                caution="25.00",
                kind=Payment.Kind.DEPOSIT,
                received_at=timezone.now(),
            )
    # The worst case for the page: stock short after damage, a stop whose city comes from its
    # zone, and a cold settings cache.
    DeliveryZoneFactory(name="Plano zone", zip_codes=["75024"])
    Booking.objects.create(
        customer_name="No city",
        email="n@example.com",
        phone="469-555-9999",
        event_date=today,
        delivery_address="4 Pine Rd",
        zip_code="75024",
        status=S.PAID,
    )
    StockAdjustmentFactory(variant=variants[0], quantity=499)
    site_settings.get()
    site_settings.forget()
    return today


def count(client, url, *args, **params):
    with CaptureQueriesContext(connection) as queries:
        response = client.get(reverse(url, args=list(args)), params)
    assert response.status_code == 200
    return len(queries)


def test_dashboard_stays_under_15_queries_for_the_owner(busy):
    owner = staff("amara", "Owner")
    client = Client()
    client.force_login(owner)
    used = count(client, "admin:index")
    assert used <= BUDGET, used


def test_dashboard_stays_under_15_queries_for_a_superuser_and_for_office(busy):
    for name, group, superuser in (("root", None, True), ("olu", "Office", False)):
        client = Client()
        client.force_login(staff(name, group, superuser))
        used = count(client, "admin:index")
        assert used <= BUDGET, (name, used)


def test_crew_schedule_stays_under_15_queries(busy):
    crew = staff("tunde", "Crew")
    client = Client()
    client.force_login(crew)
    used = count(client, "dashboard:crew_day", busy.isoformat())
    assert used <= BUDGET, used
    owner = staff("amara", "Owner")
    client.force_login(owner)
    used = count(client, "dashboard:crew_day", busy.isoformat())
    assert used <= BUDGET, used


def test_crew_schedule_has_no_per_stop_queries(busy):
    """The query count does not grow with the number of stops on the page."""
    client = Client()
    client.force_login(staff("amara", "Owner"))
    small = count(client, "dashboard:crew_day", (busy + timedelta(days=30)).isoformat())
    full = count(client, "dashboard:crew_day", busy.isoformat())
    assert full - small <= 4


def test_calendar_and_booking_list_do_not_query_per_booking(busy):
    client = Client()
    client.force_login(staff("amara", "Owner"))
    assert count(client, "dashboard:calendar") <= 15
    assert count(client, "admin:bookings_booking_changelist") <= 30


def test_the_page_shows_real_data_not_an_empty_shell(busy):
    client = Client()
    client.force_login(staff("amara", "Owner"))
    html = client.get(reverse("admin:index")).content.decode()
    assert "Today's jobs" in html
    assert "Needs attention" in html
    assert "Customer" in html
