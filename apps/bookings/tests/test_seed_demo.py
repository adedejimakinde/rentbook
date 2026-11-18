"""seed_demo fills a development database for demos, and refuses to run otherwise."""

from collections import Counter
from datetime import timedelta
from io import StringIO

import pytest
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.bookings.models import Booking, BookingItem, DeliveryZone
from apps.bookings.services import availability
from apps.catalogue.models import Item, ItemVariant, Package
from apps.core.models import Review
from apps.payments.models import Payment

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture(autouse=True)
def debug_on(settings):
    """pytest turns DEBUG off; the command needs it on, as it is in development."""
    settings.DEBUG = True


def seed():
    out = StringIO()
    call_command("seed_demo", stdout=out)
    return out.getvalue()


def snapshot():
    return (
        Booking.objects.count(),
        Payment.objects.count(),
        BookingItem.objects.count(),
        DeliveryZone.objects.count(),
        Package.objects.count(),
        User.objects.count(),
    )


def test_refuses_to_run_when_debug_is_off(settings):
    settings.DEBUG = False
    with pytest.raises(CommandError, match="DEBUG"):
        seed()
    assert Booking.objects.count() == 0
    assert not User.objects.exists()


def test_running_twice_changes_nothing_the_second_time():
    seed()
    first = snapshot()
    output = seed()
    assert snapshot() == first
    assert "created 0 new bookings and 0 new users" in output


def test_every_variant_is_priced_with_confirmed_stock():
    seed()
    assert not ItemVariant.objects.filter(stock_confirmed=False).exists()
    assert not ItemVariant.objects.filter(stock_quantity=0).exists()
    assert not Item.objects.filter(base_price=0).exists()


def test_three_zones_with_real_dfw_zips_and_no_overlap():
    seed()
    zones = list(DeliveryZone.objects.all())
    assert len(zones) == 3
    all_zips = [code for zone in zones for code in zone.zip_codes]
    assert len(all_zips) == len(set(all_zips))
    assert all(code.startswith(("75", "76")) and len(code) == 5 for code in all_zips)
    assert "75201" in DeliveryZone.objects.get(name="Dallas core").zip_codes
    for zone in zones:
        zone.full_clean()


def test_four_packages_each_with_contents():
    seed()
    packages = Package.objects.prefetch_related("items")
    assert packages.count() == 4
    assert all(package.items.exists() and package.package_price > 0 for package in packages)


def test_bookings_span_the_next_three_weeks_in_every_status():
    seed()
    bookings = Booking.objects.all()
    assert 14 <= bookings.count() <= 16
    today = timezone.localdate()
    assert all(today <= b.event_date <= today + timedelta(days=21) for b in bookings)
    statuses = {b.status for b in bookings}
    assert statuses >= {
        Booking.Status.HELD,
        Booking.Status.DEPOSIT_PAID,
        Booking.Status.PAID,
        Booking.Status.DELIVERED,
        Booking.Status.RETURNED,
    }
    assert bookings.filter(needs_attention=True).exists()
    for booking in bookings:
        assert booking.city
        assert booking.zip_code
        assert booking.delivery_time
        assert booking.pickup_time
        assert booking.crew_notes
        assert booking.items.exists() or booking.packages.exists()


def test_held_bookings_have_a_live_hold():
    seed()
    held = Booking.objects.filter(status=Booking.Status.HELD)
    assert held.exists()
    assert all(booking.hold_expires_at > timezone.now() for booking in held)


def test_payments_match_each_bookings_status():
    seed()
    for booking in Booking.objects.all():
        if booking.status == Booking.Status.HELD:
            assert not booking.payments.exists()
        elif booking.status == Booking.Status.PAID or booking.status == Booking.Status.RETURNED:
            assert booking.balance_due == 0
        elif booking.status == Booking.Status.DEPOSIT_PAID:
            assert booking.amount_paid >= booking.deposit_amount
            assert booking.balance_due > 0
    methods = Counter(Payment.objects.values_list("method", flat=True))
    assert len(methods) >= 3
    manual = Payment.objects.filter(method__in=Payment.MANUAL_METHODS)
    assert manual.exists()
    assert all(p.reference and p.received_at and p.received_by for p in manual)


def test_no_booking_oversells_a_variant_on_any_day():
    seed()
    for variant in ItemVariant.objects.all():
        rows = BookingItem.objects.filter(variant=variant).select_related("booking")
        by_day = Counter()
        for row in rows:
            if row.booking.status in availability.STOCK_HOLDING_STATUSES:
                by_day[row.booking.event_date] += row.quantity
        assert all(total <= variant.stock_quantity for total in by_day.values()), variant


def test_owner_and_crew_can_log_in_with_the_demo_password():
    seed()
    owner = User.objects.get(username="owner")
    crew = User.objects.get(username="crew")
    assert owner.check_password("demo1234") and crew.check_password("demo1234")
    assert owner.is_staff and crew.is_staff
    assert list(owner.groups.values_list("name", flat=True)) == ["Owner"]
    assert list(crew.groups.values_list("name", flat=True)) == ["Crew"]
    assert owner.has_perm("bookings.change_booking")
    assert not crew.has_perm("bookings.change_booking")


def test_no_reviews_are_created():
    seed()
    assert not Review.objects.exists()


def test_seeded_staff_screens_load(client):
    seed()
    today = timezone.localdate().isoformat()
    for username in ("owner", "crew"):
        client.login(username=username, password="demo1234")  # noqa: S106
        for path in ("crew/" + today + "/",) + (("", "calendar/") if username == "owner" else ()):
            response = client.get(f"/{django_settings.ADMIN_URL}{path}")
            assert response.status_code == 200, (username, path)
