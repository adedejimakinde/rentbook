"""Moving a booking's date in the admin is checked against stock whoever makes the change."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.tests.factories import (
    EVENT_DATE,
    BookingFactory,
    BookingItemFactory,
    ItemVariantFactory,
)
from apps.bookings.tests.helpers import add_payment
from apps.bookings.tests.test_admin import booking_form, change_url

pytestmark = pytest.mark.django_db

D = Decimal
Status = Booking.Status
OTHER_DAY = EVENT_DATE + timedelta(days=30)


@pytest.fixture
def clerk(client):
    """Staff who may edit bookings but have no permissions on booking items at all."""
    user = get_user_model().objects.create_user("clerk", is_staff=True)
    user.user_permissions.set(
        Permission.objects.filter(
            content_type__app_label="bookings", codename__in=["view_booking", "change_booking"]
        )
    )
    client.force_login(user)
    return client


def booking_on_a_full_day_elsewhere(with_payment):
    """A booking holding the only unit, and another day where that unit is already taken."""
    variant = ItemVariantFactory(stock_quantity=1)
    BookingItemFactory(
        variant=variant, quantity=1, booking__status=Status.PAID, booking__event_date=OTHER_DAY
    )
    if with_payment:
        booking = BookingFactory(status=Status.PAID)
    else:
        booking = BookingFactory(
            status=Status.HELD, hold_expires_at=timezone.now() + timedelta(hours=2)
        )
    BookingItemFactory(booking=booking, variant=variant, quantity=1, unit_price=D("50.00"))
    if with_payment:
        add_payment(booking, "50.00")
    return booking


@pytest.mark.parametrize("with_payment", [True, False])
def test_date_change_by_staff_without_item_permissions_is_checked(clerk, with_payment):
    booking = booking_on_a_full_day_elsewhere(with_payment)
    hold = booking.hold_expires_at
    data = booking_form(booking.status, [], event_date=OTHER_DAY, hold_expires_at=hold)

    response = clerk.post(change_url(booking), data)

    assert response.status_code == 200
    errors = " ".join(response.context["adminform"].form.non_field_errors())
    assert "Not enough stock" in errors
    booking.refresh_from_db()
    assert booking.event_date == EVENT_DATE


def test_date_change_with_room_to_spare_is_saved(clerk):
    booking = booking_on_a_full_day_elsewhere(with_payment=True)
    free_day = OTHER_DAY + timedelta(days=7)

    response = clerk.post(change_url(booking), booking_form("paid", [], event_date=free_day))

    assert response.status_code == 302
    booking.refresh_from_db()
    assert booking.event_date == free_day
