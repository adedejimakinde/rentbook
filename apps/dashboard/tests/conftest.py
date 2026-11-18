"""Fixtures for the staff screens: users in each group, and bookings with money on them."""

from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.tests.helpers import add_payment, booking_worth
from apps.payments.models import Payment

D = Decimal
User = get_user_model()


def staff(username, group=None, superuser=False):
    user = User.objects.create_user(
        username=username,
        password="pw",  # noqa: S106
        is_staff=True,
        is_superuser=superuser,
        first_name=username.title(),
    )
    if group:
        user.groups.add(Group.objects.get(name=group))
    return user


@pytest.fixture
def owner(db):
    return staff("amara", "Owner")


@pytest.fixture
def office(db):
    return staff("olu", "Office")


@pytest.fixture
def crew(db):
    return staff("tunde", "Crew")


@pytest.fixture
def owner_client(owner):
    client = Client()
    client.force_login(owner)
    return client


@pytest.fixture
def crew_client(crew):
    client = Client()
    client.force_login(crew)
    return client


@pytest.fixture
def today():
    return timezone.localdate()


def make_booking(
    status=Booking.Status.PAID,
    event_date=None,
    total="100.00",
    paid=None,
    caution_fee="0.00",
    caution_paid="0.00",
    **kwargs,
):
    """A booking worth total with paid already received (all of it for PAID, none otherwise)."""
    if paid is None:
        paid = total if status in (Booking.Status.PAID, Booking.Status.DELIVERED) else "0.00"
    booking = booking_worth(
        total,
        status,
        caution_fee=caution_fee,
        event_date=event_date or date(2027, 6, 12),
        **kwargs,
    )
    if D(paid) or D(caution_paid):
        add_payment(
            booking,
            paid,
            caution=caution_paid,
            kind=Payment.Kind.FULL if paid == total else Payment.Kind.DEPOSIT,
            received_at=timezone.now(),
        )
    return booking


@pytest.fixture
def make():
    return make_booking


__all__ = ["make_booking", "staff", "time", "timedelta"]
