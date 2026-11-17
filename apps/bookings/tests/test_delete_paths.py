"""No path deletes a booking that holds stock or has money on it."""

from datetime import timedelta
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.db.models import ProtectedError
from django.urls import reverse
from django.utils import timezone

from apps.bookings.admin import BookingAdmin
from apps.bookings.models import Booking, BookingDeleteRefused
from apps.bookings.tests.factories import (
    BookingFactory,
    BookingItemFactory,
    BookingPackageFactory,
    PromoCodeFactory,
)
from apps.bookings.tests.helpers import add_payment

pytestmark = pytest.mark.django_db

Status = Booking.Status


def live_hold():
    return BookingFactory(status=Status.HELD, hold_expires_at=timezone.now() + timedelta(hours=1))


@pytest.mark.parametrize("status", [Status.DEPOSIT_PAID, Status.PAID, Status.DELIVERED])
def test_deleting_a_stock_holding_booking_is_refused(status):
    booking = BookingFactory(status=status)
    BookingItemFactory(booking=booking)

    with pytest.raises(BookingDeleteRefused):
        booking.delete()
    assert Booking.objects.filter(pk=booking.pk).exists()


def test_deleting_a_live_hold_is_refused():
    booking = live_hold()
    with pytest.raises(BookingDeleteRefused):
        booking.delete()


def test_queryset_delete_refuses_and_deletes_nothing():
    enquiry = BookingFactory(status=Status.ENQUIRY)
    paid = BookingFactory(status=Status.PAID)

    with pytest.raises(BookingDeleteRefused):
        Booking.objects.filter(pk__in=[enquiry.pk, paid.pk]).delete()

    assert Booking.objects.filter(pk__in=[enquiry.pk, paid.pk]).count() == 2


def test_deleting_a_cancelled_booking_with_payments_is_refused():
    booking = BookingFactory(status=Status.CANCELLED)
    add_payment(booking, "50.00")

    with pytest.raises((BookingDeleteRefused, ProtectedError)):
        booking.delete()
    assert Booking.objects.filter(pk=booking.pk).exists()


@pytest.mark.parametrize("status", [Status.ENQUIRY, Status.QUOTED, Status.EXPIRED])
def test_bookings_holding_nothing_can_still_be_deleted(status):
    booking = BookingFactory(status=status)
    BookingItemFactory(booking=booking)
    booking.delete()
    assert not Booking.objects.filter(pk=booking.pk).exists()


def test_lapsed_hold_can_be_deleted():
    booking = BookingFactory(status=Status.HELD, hold_expires_at=timezone.now())
    booking.delete()
    assert not Booking.objects.exists()


def test_deleting_what_a_booking_points_at_never_cascades_to_it():
    booking = BookingFactory(status=Status.PAID, promo_code=PromoCodeFactory())
    item = BookingItemFactory(booking=booking)
    line = BookingPackageFactory(booking=booking)
    user = get_user_model().objects.create_user("driver")
    Booking.objects.filter(pk=booking.pk).update(delivery_override_by=user)

    for related in (item.variant, item.variant.item, line.package, booking.promo_code, user):
        with pytest.raises((ProtectedError, BookingDeleteRefused)):
            related.delete()
    assert Booking.objects.filter(pk=booking.pk).exists()


def test_refused_delete_is_a_protected_error():
    booking = live_hold()

    with pytest.raises(ProtectedError) as raised:
        booking.delete()

    assert list(raised.value.protected_objects) == [booking]


def test_a_delete_refused_after_the_admin_permission_check_shows_an_error(admin_client):
    """A booking can be put on hold between the admin's permission check and the delete."""
    booking = live_hold()
    url = reverse("admin:bookings_booking_delete", args=[booking.pk])

    with mock.patch.object(BookingAdmin, "has_delete_permission", return_value=True):
        response = admin_client.post(url, {"post": "yes"}, follow=True)

    assert response.status_code == 200
    text = " ".join(str(message) for message in response.context["messages"])
    assert "cannot be deleted" in text
    assert Booking.objects.filter(pk=booking.pk).exists()
