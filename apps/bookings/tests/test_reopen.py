"""Reopening a cancelled booking re-checks and re-holds its stock."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.services import availability, lifecycle
from apps.bookings.tests.factories import (
    EVENT_DATE,
    BookingFactory,
    BookingItemFactory,
    ItemVariantFactory,
    PromoCodeFactory,
)
from apps.bookings.tests.helpers import add_payment, booking_worth
from apps.payments.models import Payment

pytestmark = pytest.mark.django_db

D = Decimal
Status = Booking.Status


def cancelled(total="200.00", stock=1, **kwargs):
    booking = booking_worth(total, Status.CANCELLED, stock=stock, **kwargs)
    return booking, booking.items.get().variant


def test_reopening_without_money_puts_the_booking_back_on_hold(settings):
    settings.ADMIN_HOLD_HOURS = 24
    booking, variant = cancelled()
    before = timezone.now()

    lifecycle.reopen_booking(booking)

    booking.refresh_from_db()
    assert booking.status == Status.HELD
    assert before + timedelta(hours=24) <= booking.hold_expires_at
    assert availability.available_quantity(variant, EVENT_DATE) == 0


@pytest.mark.parametrize(
    ("paid", "status"), [("100.00", Status.DEPOSIT_PAID), ("200.00", Status.PAID)]
)
def test_reopening_follows_the_money_on_the_booking(paid, status):
    booking, _ = cancelled()
    add_payment(booking, paid)

    lifecycle.reopen_booking(booking)

    booking.refresh_from_db()
    assert booking.status == status
    assert booking.hold_expires_at is None


def test_reopening_with_less_than_the_deposit_holds_and_flags():
    booking, _ = cancelled()
    add_payment(booking, "20.00")

    lifecycle.reopen_booking(booking)

    booking.refresh_from_db()
    assert booking.status == Status.HELD
    assert booking.needs_attention


def test_reopening_is_refused_when_the_stock_is_gone():
    booking, variant = cancelled()
    BookingItemFactory(variant=variant, quantity=1, booking__status=Status.PAID)

    with pytest.raises(lifecycle.BookingStateError, match="only 0 available"):
        lifecycle.reopen_booking(booking)

    booking.refresh_from_db()
    assert booking.status == Status.CANCELLED


@pytest.mark.parametrize("status", [Status.PAID, Status.HELD, Status.ENQUIRY, Status.RETURNED])
def test_only_cancelled_bookings_can_be_reopened(status):
    extra = {"hold_expires_at": timezone.now() + timedelta(hours=1)} if status == "held" else {}
    booking = BookingFactory(status=status, **extra)
    with pytest.raises(lifecycle.BookingStateError, match="Only a cancelled booking"):
        lifecycle.reopen_booking(booking)


def test_reopening_is_refused_while_money_is_moving():
    booking, _ = cancelled()
    add_payment(booking, "50.00", status=Payment.Status.PENDING, method=Payment.Method.CARD)
    with pytest.raises(lifecycle.BookingStateError, match="in progress"):
        lifecycle.reopen_booking(booking)


def test_reopening_a_past_event_is_refused():
    booking, _ = cancelled(event_date=timezone.localdate() - timedelta(days=1))
    with pytest.raises(lifecycle.BookingStateError, match="already passed"):
        lifecycle.reopen_booking(booking)


def test_reopening_takes_the_promo_use_back_when_money_is_on_it():
    promo = PromoCodeFactory(max_uses=5, uses_count=1)
    booking, _ = cancelled(promo_code=promo)
    add_payment(booking, "200.00")

    lifecycle.reopen_booking(booking)

    promo.refresh_from_db()
    booking.refresh_from_db()
    assert promo.uses_count == 2
    assert booking.promo_redeemed_at is not None


def test_admin_reopen_action(admin_client):
    booking, _ = cancelled()

    response = admin_client.post(
        reverse("admin:bookings_booking_changelist"),
        {"action": "reopen_booking", "_selected_action": [booking.pk]},
    )
    form_url = reverse("admin:bookings_booking_ledger", args=[booking.pk, "reopen"])
    assert response["Location"] == form_url

    response = admin_client.post(form_url, {})
    assert response.status_code == 302
    booking.refresh_from_db()
    assert booking.status == Status.HELD


def test_admin_reopen_shows_the_shortage(admin_client):
    booking, variant = cancelled()
    variant_name = str(ItemVariantFactory._meta.model.objects.get(pk=variant.pk))
    BookingItemFactory(variant=variant, quantity=1, booking__status=Status.PAID)

    response = admin_client.post(
        reverse("admin:bookings_booking_ledger", args=[booking.pk, "reopen"]), {}
    )

    assert response.status_code == 200
    assert variant_name in response.content.decode()
