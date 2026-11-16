"""Before any payment, saving a booking in the admin recomputes its tax and caution."""

from datetime import timedelta
from decimal import Decimal as D

import pytest
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.tests.factories import (
    BookingFactory,
    BookingItemFactory,
    ItemVariantFactory,
    PackageFactory,
    PackageItemFactory,
)
from apps.bookings.tests.helpers import add_payment
from apps.bookings.tests.test_admin import ADD_URL, booking_form, change_url

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def tax(settings):
    settings.SALES_TAX_RATE = D("10")
    settings.TAX_DELIVERY = False
    settings.TAX_SETUP = False


def chairs(caution="5.00", stock=100):
    return ItemVariantFactory(item__caution_fee=D(caution), stock_quantity=stock)


def test_tax_and_caution_come_from_the_lines_not_the_form(admin_client):
    variant = chairs()
    data = booking_form("held", [{"variant": variant, "quantity": 2}])
    data.update({"tax": "99.00", "caution_fee": "99.00"})

    assert admin_client.post(ADD_URL, data).status_code == 302

    booking = Booking.objects.get()
    assert booking.tax == D("5.00")
    assert booking.caution_fee == D("10.00")
    assert booking.total == D("55.00")


def test_discount_stays_editable_and_is_taken_off_before_tax(admin_client):
    variant = chairs()
    data = booking_form("held", [{"variant": variant, "quantity": 2}])
    data["discount"] = "10.00"

    admin_client.post(ADD_URL, data)

    booking = Booking.objects.get()
    assert booking.discount == D("10.00")
    assert booking.tax == D("4.00")
    assert booking.total == D("44.00")


def test_discount_above_the_subtotal_does_not_make_tax_negative(admin_client):
    data = booking_form("enquiry", [{"variant": chairs(), "quantity": 1}])
    data["discount"] = "80.00"

    assert admin_client.post(ADD_URL, data).status_code == 302
    assert Booking.objects.get().tax == D("0.00")


def test_delivery_and_setup_are_taxed_when_switched_on(admin_client, settings):
    settings.TAX_DELIVERY = True
    data = booking_form("held", [{"variant": chairs(), "quantity": 2}])
    data.update({"delivery_fee": "40.00", "setup_fee": "20.00"})

    admin_client.post(ADD_URL, data)

    assert Booking.objects.get().tax == D("9.00")


def test_editing_lines_reprices_an_existing_booking(admin_client):
    variant = chairs()
    booking = BookingFactory(
        status=Booking.Status.HELD,
        hold_expires_at=timezone.now() + timedelta(hours=1),
        tax=D("1.00"),
        caution_fee=D("1.00"),
    )
    item = BookingItemFactory(booking=booking, variant=variant, quantity=1)

    response = admin_client.post(
        change_url(booking),
        booking_form(
            "held",
            [{"variant": variant, "quantity": 4, "id": item.pk}],
            initial_forms=1,
            hold_expires_at=booking.hold_expires_at,
        ),
    )

    assert response.status_code == 302
    booking.refresh_from_db()
    assert booking.tax == D("10.00")
    assert booking.caution_fee == D("20.00")


def test_packages_are_taxed_and_their_units_carry_caution(admin_client):
    variant = chairs()
    package = PackageFactory()
    PackageItemFactory(package=package, variant=variant, quantity=10)

    admin_client.post(
        ADD_URL,
        booking_form(
            "held",
            [{"variant": variant, "quantity": 12, "package_quantity": 10}],
            packages=[{"package": package, "quantity": 1, "unit_price": "100.00"}],
        ),
    )

    booking = Booking.objects.get()
    assert booking.tax == D("15.00")
    assert booking.caution_fee == D("60.00")


def test_bookings_with_payments_keep_their_tax_and_caution(admin_client):
    variant = chairs()
    booking = BookingFactory(status=Booking.Status.PAID, tax=D("1.00"), caution_fee=D("1.00"))
    item = BookingItemFactory(booking=booking, variant=variant, quantity=1)
    add_payment(booking, "27.00")

    data = booking_form(
        "paid", [{"variant": variant, "quantity": 1, "id": item.pk}], initial_forms=1
    )
    data["customer_name"] = "Ada Renamed"
    assert admin_client.post(change_url(booking), data).status_code == 302

    booking.refresh_from_db()
    assert booking.customer_name == "Ada Renamed"
    assert (booking.tax, booking.caution_fee) == (D("1.00"), D("1.00"))
