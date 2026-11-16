"""The admin must not bypass the availability check."""

import pytest
from django.urls import reverse

from apps.bookings.models import Booking, BookingItem
from apps.bookings.tests.factories import (
    EVENT_DATE,
    BookingFactory,
    BookingItemFactory,
    ItemVariantFactory,
)

pytestmark = pytest.mark.django_db


def booking_form(status, items, event_date=EVENT_DATE, initial_forms=0):
    """POST data for the booking admin form with inline booking items.

    items is a list of dicts with variant, quantity and optionally id / DELETE.
    """
    data = {
        "customer_name": "Ada Planner",
        "email": "ada@example.com",
        "phone": "555-0100",
        "event_date": event_date.isoformat(),
        "delivery_address": "1 Main St, Dallas, TX",
        "status": status,
        "hold_expires_at_0": "",
        "hold_expires_at_1": "",
        "items-TOTAL_FORMS": len(items),
        "items-INITIAL_FORMS": initial_forms,
        "items-MIN_NUM_FORMS": 0,
        "items-MAX_NUM_FORMS": 1000,
    }
    for index, item in enumerate(items):
        data[f"items-{index}-variant"] = item["variant"].pk
        data[f"items-{index}-quantity"] = item["quantity"]
        data[f"items-{index}-unit_price"] = "25.00"
        if "id" in item:
            data[f"items-{index}-id"] = item["id"]
        if item.get("DELETE"):
            data[f"items-{index}-DELETE"] = "on"
    return data


def change_url(booking):
    return reverse("admin:bookings_booking_change", args=[booking.pk])


ADD_URL = reverse("admin:bookings_booking_add")


def errors_in(response):
    formset = response.context["inline_admin_formsets"][0].formset
    return " ".join(formset.non_form_errors())


def test_creating_a_paid_booking_beyond_stock_shows_error(admin_client):
    variant = ItemVariantFactory(stock_quantity=3, item__name="Gold Chiavari Chair")
    BookingItemFactory(variant=variant, quantity=2, booking__status=Booking.Status.PAID)

    response = admin_client.post(
        ADD_URL, booking_form("paid", [{"variant": variant, "quantity": 2}])
    )

    assert response.status_code == 200
    errors = errors_in(response)
    assert "Not enough stock on Jun 12, 2027" in errors
    assert "Gold Chiavari Chair (Default): requested 2, only 1 available" in errors
    assert "Not enough stock" in response.content.decode()
    assert Booking.objects.count() == 1
    assert BookingItem.objects.count() == 1


def test_creating_a_booking_within_stock_saves(admin_client):
    variant = ItemVariantFactory(stock_quantity=3)

    response = admin_client.post(
        ADD_URL, booking_form("held", [{"variant": variant, "quantity": 3}])
    )

    assert response.status_code == 302
    booking = Booking.objects.get()
    assert booking.status == Booking.Status.HELD
    assert booking.items.get().quantity == 3


def test_enquiry_beyond_stock_is_allowed_because_it_holds_nothing(admin_client):
    variant = ItemVariantFactory(stock_quantity=1)

    response = admin_client.post(
        ADD_URL, booking_form("enquiry", [{"variant": variant, "quantity": 5}])
    )

    assert response.status_code == 302


@pytest.mark.parametrize("status", ["held", "deposit_paid", "paid", "delivered"])
def test_moving_a_booking_into_a_stock_holding_status_is_checked(admin_client, status):
    variant = ItemVariantFactory(stock_quantity=1)
    BookingItemFactory(variant=variant, quantity=1, booking__status=Booking.Status.PAID)
    enquiry = BookingFactory(status=Booking.Status.ENQUIRY)
    item = BookingItemFactory(booking=enquiry, variant=variant, quantity=1)

    response = admin_client.post(
        change_url(enquiry),
        booking_form(status, [{"variant": variant, "quantity": 1, "id": item.pk}], initial_forms=1),
    )

    assert response.status_code == 200
    assert "requested 1, only 0 available" in errors_in(response)
    enquiry.refresh_from_db()
    assert enquiry.status == Booking.Status.ENQUIRY


def test_raising_a_quantity_beyond_stock_is_rejected(admin_client):
    variant = ItemVariantFactory(stock_quantity=5)
    BookingItemFactory(variant=variant, quantity=2, booking__status=Booking.Status.DEPOSIT_PAID)
    booking = BookingFactory(status=Booking.Status.PAID)
    item = BookingItemFactory(booking=booking, variant=variant, quantity=3)

    response = admin_client.post(
        change_url(booking),
        booking_form("paid", [{"variant": variant, "quantity": 4, "id": item.pk}], initial_forms=1),
    )

    assert response.status_code == 200
    assert "requested 4, only 3 available" in errors_in(response)
    item.refresh_from_db()
    assert item.quantity == 3


def test_adding_an_item_beyond_stock_is_rejected(admin_client):
    chairs = ItemVariantFactory(stock_quantity=5)
    tables = ItemVariantFactory(stock_quantity=1)
    BookingItemFactory(variant=tables, quantity=1, booking__status=Booking.Status.PAID)
    booking = BookingFactory(status=Booking.Status.PAID)
    item = BookingItemFactory(booking=booking, variant=chairs, quantity=1)

    response = admin_client.post(
        change_url(booking),
        booking_form(
            "paid",
            [
                {"variant": chairs, "quantity": 1, "id": item.pk},
                {"variant": tables, "quantity": 1},
            ],
            initial_forms=1,
        ),
    )

    assert response.status_code == 200
    assert "requested 1, only 0 available" in errors_in(response)
    assert booking.items.count() == 1


def test_moving_the_event_date_onto_a_full_day_is_rejected(admin_client):
    variant = ItemVariantFactory(stock_quantity=1)
    BookingItemFactory(variant=variant, quantity=1, booking__status=Booking.Status.PAID)
    other_day = EVENT_DATE.replace(day=20)
    booking = BookingFactory(status=Booking.Status.PAID, event_date=other_day)
    item = BookingItemFactory(booking=booking, variant=variant, quantity=1)

    response = admin_client.post(
        change_url(booking),
        booking_form("paid", [{"variant": variant, "quantity": 1, "id": item.pk}], initial_forms=1),
    )

    assert response.status_code == 200
    booking.refresh_from_db()
    assert booking.event_date == other_day


def test_a_booking_does_not_count_against_itself(admin_client):
    variant = ItemVariantFactory(stock_quantity=3)
    booking = BookingFactory(status=Booking.Status.PAID)
    item = BookingItemFactory(booking=booking, variant=variant, quantity=3)

    response = admin_client.post(
        change_url(booking),
        booking_form(
            "delivered", [{"variant": variant, "quantity": 3, "id": item.pk}], initial_forms=1
        ),
    )

    assert response.status_code == 302
    booking.refresh_from_db()
    assert booking.status == Booking.Status.DELIVERED


def test_deleted_rows_are_not_counted(admin_client):
    variant = ItemVariantFactory(stock_quantity=1)
    BookingItemFactory(variant=variant, quantity=1, booking__status=Booking.Status.PAID)
    booking = BookingFactory(status=Booking.Status.ENQUIRY)
    item = BookingItemFactory(booking=booking, variant=variant, quantity=1)

    response = admin_client.post(
        change_url(booking),
        booking_form(
            "paid",
            [{"variant": variant, "quantity": 1, "id": item.pk, "DELETE": True}],
            initial_forms=1,
        ),
    )

    assert response.status_code == 302
    assert booking.items.count() == 0


def test_item_quantity_must_be_at_least_one(admin_client):
    variant = ItemVariantFactory(stock_quantity=5)

    response = admin_client.post(
        ADD_URL, booking_form("enquiry", [{"variant": variant, "quantity": 0}])
    )

    assert response.status_code == 200
    assert Booking.objects.count() == 0
