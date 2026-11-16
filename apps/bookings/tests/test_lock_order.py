"""Every path that locks both a booking and stock locks the booking first.

The ledger locks booking then stock; any path taking them the other way round
could deadlock against it.
"""

from contextlib import contextmanager
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.services import lifecycle
from apps.bookings.tests.factories import BookingFactory, BookingItemFactory, ItemVariantFactory
from apps.bookings.tests.helpers import add_payment
from apps.bookings.tests.test_admin import booking_form, change_url
from apps.payments.services import ledger

pytestmark = pytest.mark.django_db


@contextmanager
def locked_tables():
    """Collect, in order, the table each SELECT ... FOR UPDATE locks."""
    tables = []

    def capture(execute, sql, params, many, context):
        if "FOR UPDATE" in sql:
            tables.append(sql.split("FROM", 1)[1].split()[0].strip('"'))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(capture):
        yield tables


def assert_booking_first(tables):
    assert "bookings_booking" in tables
    assert "catalogue_itemvariant" in tables
    assert tables.index("bookings_booking") < tables.index("catalogue_itemvariant")


def held(stock=3):
    variant = ItemVariantFactory(stock_quantity=stock)
    booking = BookingFactory(
        status=Booking.Status.HELD, hold_expires_at=timezone.now() + timedelta(hours=1)
    )
    item = BookingItemFactory(booking=booking, variant=variant, quantity=1)
    return booking, variant, item


def test_admin_change_view_locks_the_booking_before_stock(admin_client):
    booking, variant, item = held()
    data = booking_form(
        "held",
        [{"variant": variant, "quantity": 2, "id": item.pk}],
        initial_forms=1,
        hold_expires_at=booking.hold_expires_at,
    )

    with locked_tables() as tables:
        response = admin_client.post(change_url(booking), data)

    assert response.status_code == 302
    assert_booking_first(tables)


def test_change_item_locks_the_booking_before_stock():
    booking, variant, _ = held()
    add_payment(booking, "10.00")

    with locked_tables() as tables:
        lifecycle.change_item(booking, variant, quantity=2)

    assert_booking_first(tables)


def test_reopen_locks_the_booking_before_stock():
    booking, _, _ = held()
    lifecycle.cancel_booking(booking)

    with locked_tables() as tables:
        lifecycle.reopen_booking(booking)

    assert_booking_first(tables)


def test_late_ach_recheck_locks_the_booking_before_stock():
    booking, _, _ = held()
    Booking.objects.filter(pk=booking.pk).update(hold_expires_at=timezone.now())
    payment = add_payment(booking, "10.00", status="pending", method="card")

    with locked_tables() as tables:
        ledger.record_processing(payment.pk)

    assert_booking_first(tables)
