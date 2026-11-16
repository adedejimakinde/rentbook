"""The admin must not bypass the availability check."""

from datetime import timedelta
from decimal import Decimal as D

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking, BookingItem, BookingPackage
from apps.bookings.services import availability
from apps.bookings.tests.factories import (
    EVENT_DATE,
    BookingFactory,
    BookingItemFactory,
    BookingPackageFactory,
    ItemVariantFactory,
    PackageFactory,
    PackageItemFactory,
)

pytestmark = pytest.mark.django_db


def booking_form(
    status,
    items,
    event_date=EVENT_DATE,
    initial_forms=0,
    hold_expires_at=None,
    packages=(),
    initial_packages=0,
):
    """POST data for the booking admin form with inline booking items and packages.

    items is a list of dicts with variant, quantity and optionally id /
    package_quantity / unit_price / DELETE. packages is a list of dicts with
    package, quantity and optionally id / unit_price / DELETE.
    """
    data = {
        "customer_name": "Ada Planner",
        "email": "ada@example.com",
        "phone": "555-0100",
        "event_date": event_date.isoformat(),
        "delivery_address": "1 Main St, Dallas, TX",
        "status": status,
        "delivery_fee": "0.00",
        "setup_fee": "0.00",
        "discount": "0.00",
        "tax": "0.00",
        "packages-TOTAL_FORMS": len(packages),
        "packages-INITIAL_FORMS": initial_packages,
        "packages-MIN_NUM_FORMS": 0,
        "packages-MAX_NUM_FORMS": 1000,
        "caution_fee": "0.00",
        "hold_expires_at_0": f"{hold_expires_at:%Y-%m-%d}" if hold_expires_at else "",
        "hold_expires_at_1": f"{hold_expires_at:%H:%M:%S}" if hold_expires_at else "",
        "items-TOTAL_FORMS": len(items),
        "items-INITIAL_FORMS": initial_forms,
        "items-MIN_NUM_FORMS": 0,
        "items-MAX_NUM_FORMS": 1000,
    }
    for index, item in enumerate(items):
        data[f"items-{index}-variant"] = item["variant"].pk
        data[f"items-{index}-quantity"] = item["quantity"]
        data[f"items-{index}-unit_price"] = item.get("unit_price", "25.00")
        data[f"items-{index}-package_quantity"] = item.get("package_quantity", 0)
        if "id" in item:
            data[f"items-{index}-id"] = item["id"]
        if item.get("DELETE"):
            data[f"items-{index}-DELETE"] = "on"
    for index, line in enumerate(packages):
        data[f"packages-{index}-package"] = line["package"].pk
        data[f"packages-{index}-name"] = line["package"].name
        data[f"packages-{index}-quantity"] = line["quantity"]
        data[f"packages-{index}-unit_price"] = line.get("unit_price", "100.00")
        if "id" in line:
            data[f"packages-{index}-id"] = line["id"]
        if line.get("DELETE"):
            data[f"packages-{index}-DELETE"] = "on"
    return data


def change_url(booking):
    return reverse("admin:bookings_booking_change", args=[booking.pk])


ADD_URL = reverse("admin:bookings_booking_add")


def errors_in(response):
    formset = response.context["inline_admin_formsets"][0].formset
    return " ".join(formset.non_form_errors())


def test_creating_a_held_booking_beyond_stock_shows_error(admin_client):
    variant = ItemVariantFactory(stock_quantity=3, item__name="Gold Chiavari Chair")
    BookingItemFactory(variant=variant, quantity=2, booking__status=Booking.Status.PAID)

    response = admin_client.post(
        ADD_URL, booking_form("held", [{"variant": variant, "quantity": 2}])
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


def test_admin_hold_without_expiry_gets_the_default(admin_client, settings):
    assert settings.ADMIN_HOLD_HOURS == 48
    settings.ADMIN_HOLD_HOURS = 24
    variant = ItemVariantFactory(stock_quantity=3)
    before = timezone.now()

    response = admin_client.post(
        ADD_URL, booking_form("held", [{"variant": variant, "quantity": 1}])
    )

    assert response.status_code == 302
    expected = before + timedelta(hours=24)
    booking = Booking.objects.get()
    assert expected <= booking.hold_expires_at <= expected + timedelta(seconds=5)
    assert availability.available_quantity(variant, EVENT_DATE) == 2


def test_admin_hold_expiry_cannot_be_cleared(admin_client):
    variant = ItemVariantFactory(stock_quantity=3)
    booking = BookingFactory(
        status=Booking.Status.HELD, hold_expires_at=timezone.now() + timedelta(minutes=5)
    )
    item = BookingItemFactory(booking=booking, variant=variant, quantity=1)
    before = timezone.now()

    response = admin_client.post(
        change_url(booking),
        booking_form("held", [{"variant": variant, "quantity": 1, "id": item.pk}], initial_forms=1),
    )

    assert response.status_code == 302
    booking.refresh_from_db()
    assert booking.hold_expires_at >= before + timedelta(hours=48)


def test_admin_hold_keeps_a_later_expiry_chosen_by_staff(admin_client):
    variant = ItemVariantFactory(stock_quantity=3)
    chosen = timezone.localtime(timezone.now() + timedelta(days=7)).replace(microsecond=0)

    response = admin_client.post(
        ADD_URL,
        booking_form("held", [{"variant": variant, "quantity": 1}], hold_expires_at=chosen),
    )

    assert response.status_code == 302
    assert Booking.objects.get().hold_expires_at == chosen


def test_admin_does_not_add_an_expiry_to_other_statuses(admin_client):
    variant = ItemVariantFactory(stock_quantity=3)

    admin_client.post(ADD_URL, booking_form("quoted", [{"variant": variant, "quantity": 1}]))

    assert Booking.objects.get().hold_expires_at is None


def test_database_rejects_a_held_booking_without_expiry():
    with pytest.raises(IntegrityError), transaction.atomic():
        BookingFactory(status=Booking.Status.HELD, hold_expires_at=None)


def test_enquiry_beyond_stock_is_allowed_because_it_holds_nothing(admin_client):
    variant = ItemVariantFactory(stock_quantity=1)

    response = admin_client.post(
        ADD_URL, booking_form("enquiry", [{"variant": variant, "quantity": 5}])
    )

    assert response.status_code == 302


def test_moving_a_booking_onto_hold_is_checked(admin_client):
    status = "held"
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
    booking = BookingFactory(status=Booking.Status.ENQUIRY)
    item = BookingItemFactory(booking=booking, variant=variant, quantity=3)

    response = admin_client.post(
        change_url(booking),
        booking_form("held", [{"variant": variant, "quantity": 3, "id": item.pk}], initial_forms=1),
    )

    assert response.status_code == 302
    booking.refresh_from_db()
    assert booking.status == Booking.Status.HELD


def test_deleted_rows_are_not_counted(admin_client):
    variant = ItemVariantFactory(stock_quantity=1)
    BookingItemFactory(variant=variant, quantity=1, booking__status=Booking.Status.PAID)
    booking = BookingFactory(status=Booking.Status.ENQUIRY)
    item = BookingItemFactory(booking=booking, variant=variant, quantity=1)

    response = admin_client.post(
        change_url(booking),
        booking_form(
            "held",
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


def party_package(chairs):
    """A package of 10 chairs. Returns (package, chairs variant)."""
    package = PackageFactory(name="Party Pack")
    PackageItemFactory(package=package, variant=chairs, quantity=10)
    return package


def test_creating_a_booking_with_a_package_saves_its_lines(admin_client):
    chairs = ItemVariantFactory(stock_quantity=20)
    package = party_package(chairs)

    response = admin_client.post(
        ADD_URL,
        booking_form(
            "held",
            [{"variant": chairs, "quantity": 12, "package_quantity": 10}],
            packages=[{"package": package, "quantity": 1}],
        ),
    )

    assert response.status_code == 302
    booking = Booking.objects.get()
    line = booking.packages.get()
    assert (line.package, line.quantity, line.unit_price) == (package, 1, D("100.00"))
    assert booking.items.get().package_quantity == 10
    assert booking.total == D("150.00")
    assert availability.available_quantity(chairs, EVENT_DATE) == 8


def test_package_units_count_against_stock_in_the_admin(admin_client):
    chairs = ItemVariantFactory(stock_quantity=10)
    package = party_package(chairs)
    BookingItemFactory(variant=chairs, quantity=5, booking__status=Booking.Status.PAID)

    response = admin_client.post(
        ADD_URL,
        booking_form(
            "held",
            [{"variant": chairs, "quantity": 10, "package_quantity": 10}],
            packages=[{"package": package, "quantity": 1}],
        ),
    )

    assert response.status_code == 200
    assert "only 5 available" in errors_in(response)
    assert BookingPackage.objects.count() == 0


def test_editing_and_removing_a_package_line(admin_client):
    chairs = ItemVariantFactory(stock_quantity=30)
    package = party_package(chairs)
    booking = BookingFactory(
        status=Booking.Status.HELD, hold_expires_at=timezone.now() + timedelta(hours=1)
    )
    item = BookingItemFactory(booking=booking, variant=chairs, quantity=10, package_quantity=10)
    line = BookingPackageFactory(booking=booking, package=package)
    common = {"initial_forms": 1, "initial_packages": 1, "hold_expires_at": booking.hold_expires_at}

    response = admin_client.post(
        change_url(booking),
        booking_form(
            "held",
            [{"variant": chairs, "quantity": 20, "package_quantity": 20, "id": item.pk}],
            packages=[{"package": package, "quantity": 2, "id": line.pk}],
            **common,
        ),
    )
    assert response.status_code == 302
    assert booking.packages.get().quantity == 2
    assert booking.total == D("200.00")

    response = admin_client.post(
        change_url(booking),
        booking_form(
            "held",
            [{"variant": chairs, "quantity": 20, "id": item.pk}],
            packages=[{"package": package, "quantity": 2, "id": line.pk, "DELETE": True}],
            **common,
        ),
    )
    assert response.status_code == 302
    assert not booking.packages.exists()
    assert booking.total == D("500.00")


def test_package_line_quantity_must_be_at_least_one(admin_client):
    chairs = ItemVariantFactory(stock_quantity=30)
    package = party_package(chairs)

    response = admin_client.post(
        ADD_URL,
        booking_form(
            "held",
            [{"variant": chairs, "quantity": 1}],
            packages=[{"package": package, "quantity": 0}],
        ),
    )

    assert response.status_code == 200
    assert BookingPackage.objects.count() == 0
