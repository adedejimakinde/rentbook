"""Once a booking has a payment, the admin cannot change its status, money or lines."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking, BookingPackage
from apps.bookings.tests.factories import (
    EVENT_DATE,
    BookingFactory,
    BookingItemFactory,
    ItemVariantFactory,
    PackageFactory,
)
from apps.bookings.tests.helpers import add_payment, booking_worth
from apps.bookings.tests.test_admin import booking_form, change_url
from apps.payments.models import Payment

pytestmark = pytest.mark.django_db

D = Decimal
Status = Booking.Status
CHANGELIST_URL = reverse("admin:bookings_booking_changelist")


def delete_url(booking):
    return reverse("admin:bookings_booking_delete", args=[booking.pk])


def action_url(booking, action):
    return reverse("admin:bookings_booking_ledger", args=[booking.pk, action])


def tampered_form(booking, status):
    """An edit that tries to change the status, every money field and the item line."""
    item = booking.items.get()
    data = booking_form(
        status, [{"variant": item.variant, "quantity": 5, "id": item.pk}], initial_forms=1
    )
    data.update(
        {
            "delivery_fee": "1.00",
            "setup_fee": "2.00",
            "discount": "150.00",
            "tax": "3.00",
            "caution_fee": "0.00",
            "items-0-unit_price": "0.01",
            "items-0-package_quantity": 1,
            "customer_name": "Ada Renamed",
        }
    )
    return data


@pytest.mark.parametrize("status", ["cancelled", "enquiry", "returned", "expired"])
def test_paid_booking_status_money_and_lines_cannot_be_edited(admin_client, status):
    booking = booking_worth("200.00", Status.PAID, caution_fee="75.00", stock=1)
    add_payment(booking, "200.00", caution="75.00")
    variant = booking.items.get().variant

    response = admin_client.post(change_url(booking), tampered_form(booking, status))

    assert response.status_code == 302
    booking.refresh_from_db()
    item = booking.items.get()
    assert booking.customer_name == "Ada Renamed"
    assert booking.status == Status.PAID
    assert (booking.delivery_fee, booking.setup_fee, booking.discount, booking.tax) == (0, 0, 0, 0)
    assert booking.caution_fee == D("75.00")
    assert (item.quantity, item.unit_price, item.package_quantity) == (1, D("200.00"), 0)
    assert booking.total == D("200.00")
    assert booking.balance_due == D("0.00")
    assert variant.booking_items.count() == 1


def test_lines_cannot_be_added_or_deleted_on_a_booking_with_payments(admin_client):
    booking = booking_worth("200.00", Status.PAID)
    add_payment(booking, "200.00")
    item = booking.items.get()
    package = PackageFactory()
    data = booking_form(
        "paid",
        [
            {"variant": item.variant, "quantity": 1, "id": item.pk, "DELETE": True},
            {"variant": ItemVariantFactory(), "quantity": 1},
        ],
        initial_forms=1,
    )
    data.update(
        {
            "packages-TOTAL_FORMS": 1,
            "packages-0-package": package.pk,
            "packages-0-name": "Free package",
            "packages-0-quantity": 1,
            "packages-0-unit_price": "0.00",
        }
    )

    admin_client.post(change_url(booking), data)

    assert list(booking.items.all()) == [item]
    assert not BookingPackage.objects.exists()


def test_an_open_checkout_also_locks_the_booking(admin_client):
    """The checkout was opened for the old total, so the total must not move under it."""
    booking = booking_worth(
        "200.00", Status.HELD, hold_expires_at=timezone.now() + timedelta(minutes=15)
    )
    add_payment(booking, "200.00", status=Payment.Status.PENDING, kind=Payment.Kind.FULL)

    admin_client.post(change_url(booking), tampered_form(booking, "enquiry"))

    booking.refresh_from_db()
    assert booking.status == Status.HELD
    assert booking.total == D("200.00")


def test_a_failed_payment_alone_does_not_lock_the_booking(admin_client):
    booking = booking_worth("200.00", Status.QUOTED)
    add_payment(booking, "200.00", status=Payment.Status.FAILED, kind=Payment.Kind.FULL)
    item = booking.items.get()
    data = booking_form(
        "quoted", [{"variant": item.variant, "quantity": 2, "id": item.pk}], initial_forms=1
    )

    assert admin_client.post(change_url(booking), data).status_code == 302

    assert booking.items.get().quantity == 2


@pytest.mark.parametrize("status", ["deposit_paid", "paid", "delivered", "returned", "cancelled"])
def test_money_statuses_cannot_be_set_by_hand_on_a_booking_without_payments(admin_client, status):
    booking = booking_worth("200.00", Status.ENQUIRY)
    item = booking.items.get()
    data = booking_form(
        status, [{"variant": item.variant, "quantity": 1, "id": item.pk}], initial_forms=1
    )

    response = admin_client.post(change_url(booking), data)

    assert response.status_code == 200
    assert "cannot be set by hand" in response.context["adminform"].form.errors["status"][0]
    booking.refresh_from_db()
    assert booking.status == Status.ENQUIRY


def test_moving_the_date_of_a_paid_booking_is_still_checked_against_stock(admin_client):
    variant = ItemVariantFactory(stock_quantity=1)
    other_day = EVENT_DATE + timedelta(days=30)
    BookingItemFactory(
        variant=variant, quantity=1, booking__status=Status.PAID, booking__event_date=other_day
    )
    booking = BookingFactory(status=Status.PAID)
    item = BookingItemFactory(booking=booking, variant=variant, quantity=1, unit_price=D("50.00"))
    add_payment(booking, "50.00")
    data = booking_form(
        "paid",
        [{"variant": variant, "quantity": 1, "id": item.pk}],
        event_date=other_day,
        initial_forms=1,
    )

    response = admin_client.post(change_url(booking), data)

    assert response.status_code == 200
    booking.refresh_from_db()
    assert booking.event_date == EVENT_DATE


@pytest.mark.parametrize(
    "status", [Status.HELD, Status.DEPOSIT_PAID, Status.PAID, Status.DELIVERED]
)
def test_a_booking_holding_stock_cannot_be_deleted(admin_client, status):
    booking = booking_worth(
        "200.00",
        status,
        hold_expires_at=timezone.now() + timedelta(minutes=15) if status == Status.HELD else None,
    )

    response = admin_client.post(delete_url(booking), {"post": "yes"})

    assert response.status_code == 403
    assert Booking.objects.filter(pk=booking.pk).exists()
    assert booking.items.count() == 1


def test_a_booking_with_a_payment_cannot_be_deleted(admin_client):
    booking = booking_worth("200.00", Status.CANCELLED)
    add_payment(booking, "200.00", status=Payment.Status.FAILED)

    assert admin_client.post(delete_url(booking), {"post": "yes"}).status_code == 403
    assert Booking.objects.filter(pk=booking.pk).exists()


def test_an_enquiry_without_payments_can_still_be_deleted(admin_client):
    booking = booking_worth("200.00", Status.ENQUIRY)

    assert admin_client.post(delete_url(booking), {"post": "yes"}).status_code == 302
    assert not Booking.objects.filter(pk=booking.pk).exists()


def test_bulk_delete_is_not_offered_for_bookings(admin_client):
    held = booking_worth(
        "200.00", Status.HELD, hold_expires_at=timezone.now() + timedelta(minutes=15)
    )
    data = {"action": "delete_selected", "_selected_action": [held.pk], "post": "yes"}

    admin_client.post(CHANGELIST_URL, data)

    assert Booking.objects.filter(pk=held.pk).exists()
    assert held.items.count() == 1


def test_admin_delivers_with_an_override_note(admin_client, admin_user):
    booking = booking_worth("200.00", Status.DEPOSIT_PAID)
    add_payment(booking, "100.00")
    url = action_url(booking, "mark-delivered")

    refused = admin_client.post(url, {"override_note": ""})
    assert refused.status_code == 200
    assert "$100.00 of the rental is still owed" in refused.content.decode()
    booking.refresh_from_db()
    assert booking.status == Status.DEPOSIT_PAID

    assert admin_client.post(url, {"override_note": "Cash on delivery"}).status_code == 302
    booking.refresh_from_db()
    assert booking.status == Status.DELIVERED
    assert booking.delivery_override_by == admin_user
    assert booking.needs_attention


def test_admin_cancel_return_and_change_item_actions(admin_client):
    booking = booking_worth("100.00", Status.PAID)
    add_payment(booking, "100.00")
    variant = booking.items.get().variant

    admin_client.post(action_url(booking, "change-item"), {"variant": variant.pk, "quantity": 2})
    booking.refresh_from_db()
    assert (booking.items.get().quantity, booking.status) == (2, Status.DEPOSIT_PAID)

    refused = admin_client.post(action_url(booking, "mark-returned"), {})
    assert "Only a delivered booking" in refused.content.decode()

    assert admin_client.post(action_url(booking, "cancel"), {}).status_code == 302
    booking.refresh_from_db()
    assert booking.status == Status.CANCELLED
    assert booking.needs_attention


def test_booking_change_page_renders_locked_and_unlocked(admin_client):
    locked = booking_worth("200.00", Status.PAID)
    add_payment(locked, "200.00")
    unlocked = booking_worth("200.00", Status.ENQUIRY)

    assert admin_client.get(change_url(locked)).status_code == 200
    assert admin_client.get(change_url(unlocked)).status_code == 200
    assert admin_client.get(action_url(locked, "mark-delivered")).status_code == 200
