"""Quotes and reservations need the event day to start at least MIN_NOTICE_HOURS from now.

The notice is measured to the start of the event day in the business's time
zone, so with 24 hours' notice tomorrow is always too soon and the day after
is always fine, whatever the time is now.
"""

from datetime import timedelta
from decimal import Decimal as D

import pytest
from django.utils import timezone

from apps.bookings.services import availability, quote
from apps.bookings.services.quote import LineRequest, QuoteNotBookable
from apps.bookings.tests.factories import DeliveryZoneFactory, ItemVariantFactory

pytestmark = pytest.mark.django_db

CUSTOMER = {
    "customer_name": "Ada Planner",
    "email": "ada@example.com",
    "phone": "555-0100",
    "delivery_address": "1 Main St, Dallas, TX 75001",
}


def in_days(days):
    return timezone.localdate() + timedelta(days=days)


@pytest.fixture
def variant(settings):
    settings.SALES_TAX_RATE = D("0")
    DeliveryZoneFactory(zip_codes=["75001"])
    return ItemVariantFactory(stock_quantity=10)


def codes(result):
    return [error.code for error in result.errors]


def test_default_notice_is_24_hours(settings):
    assert settings.MIN_NOTICE_HOURS == 24


@pytest.mark.parametrize("days", [-30, -1, 0, 1])
def test_quote_refuses_past_and_short_notice_dates(variant, days):
    result = quote.build_quote(
        [LineRequest(variant_id=variant.pk, quantity=1)], in_days(days), "75001"
    )
    assert quote.EVENT_TOO_SOON in codes(result)
    assert not result.is_bookable


def test_past_dates_get_their_own_message(variant):
    result = quote.build_quote(
        [LineRequest(variant_id=variant.pk, quantity=1)], in_days(-1), "75001"
    )
    (error,) = [e for e in result.errors if e.code == quote.EVENT_TOO_SOON]
    assert "in the past" in error.message


def test_quote_accepts_a_date_with_enough_notice(variant):
    result = quote.build_quote(
        [LineRequest(variant_id=variant.pk, quantity=1)], in_days(2), "75001"
    )
    assert result.is_bookable


def test_notice_follows_the_setting(variant, settings):
    settings.MIN_NOTICE_HOURS = 72
    lines = [LineRequest(variant_id=variant.pk, quantity=1)]
    assert quote.EVENT_TOO_SOON in codes(quote.build_quote(lines, in_days(3), "75001"))
    assert quote.build_quote(lines, in_days(4), "75001").is_bookable


@pytest.mark.parametrize("days", [-1, 0, 1])
def test_reserve_refuses_short_notice(variant, days):
    with pytest.raises(availability.EventDateError):
        availability.reserve([(variant, 1)], in_days(days), **CUSTOMER)
    assert availability.available_quantity(variant, in_days(days)) == 10


def test_reserve_accepts_enough_notice(variant):
    booking = availability.reserve([(variant, 1)], in_days(2), **CUSTOMER)
    assert booking.event_date == in_days(2)


def test_booking_from_quote_refuses_short_notice(variant):
    with pytest.raises(QuoteNotBookable) as raised:
        quote.create_booking_from_quote(
            [LineRequest(variant_id=variant.pk, quantity=1)], in_days(1), "75001", **CUSTOMER
        )
    assert quote.EVENT_TOO_SOON in codes(raised.value.quote)
