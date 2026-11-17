"""Line changes recompute tax and caution; tax exemption needs a certificate and a permission."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.services import lifecycle, quote
from apps.bookings.tests.factories import BookingFactory, BookingItemFactory, ItemVariantFactory
from apps.bookings.tests.helpers import add_payment
from apps.payments.models import Payment
from apps.payments.tests.helpers import start_checkout

pytestmark = pytest.mark.django_db

D = Decimal
Status = Booking.Status


@pytest.fixture(autouse=True)
def tax(settings):
    settings.SALES_TAX_RATE = D("10")
    settings.TAX_DELIVERY = False
    settings.TAX_SETUP = False


@pytest.fixture
def staff():
    return get_user_model().objects.create_user("owner", is_staff=True)


def paid_booking(quantity=2, caution_per_unit="25.00", status=Status.PAID):
    """Lines of $50 units with caution per unit; paid in full with the caution collected."""
    variant = ItemVariantFactory(item__caution_fee=D(caution_per_unit), stock_quantity=10)
    booking = BookingFactory(status=status)
    BookingItemFactory(booking=booking, variant=variant, quantity=quantity, unit_price=D("50.00"))
    quote.reprice_booking(booking)
    booking.refresh_from_db()
    add_payment(booking, booking.total, caution=booking.caution_fee, kind=Payment.Kind.FULL)
    return booking, variant


def test_a_line_change_recomputes_the_tax(staff):
    booking, variant = paid_booking()
    assert booking.tax == D("10.00")

    lifecycle.change_item(booking, variant, quantity=3)

    booking.refresh_from_db()
    assert booking.tax == D("15.00")
    assert booking.total == D("165.00")
    assert booking.status == Status.DEPOSIT_PAID
    assert booking.needs_attention


def test_more_caution_owed_is_collected_with_the_balance_outside_amount_paid(staff):
    booking, variant = paid_booking()

    lifecycle.change_item(booking, variant, quantity=3)

    booking.refresh_from_db()
    assert booking.caution_fee == D("75.00")
    assert booking.caution_due == D("25.00")
    assert booking.caution_excess == D("0.00")
    payment, call = start_checkout(booking, Payment.Kind.BALANCE)
    assert payment.amount == D("55.00")
    assert payment.caution_amount == D("25.00")
    assert [line["price_data"]["unit_amount"] for line in call["line_items"]] == [5500, 2500]
    assert booking.amount_paid == D("110.00")


def test_less_caution_owed_flags_the_booking_for_a_caution_refund(staff):
    booking, variant = paid_booking()
    Booking.objects.filter(pk=booking.pk).update(needs_attention=False)

    lifecycle.change_item(booking, variant, quantity=1)

    booking.refresh_from_db()
    assert booking.caution_fee == D("25.00")
    assert booking.caution_held == D("50.00")
    assert booking.caution_excess == D("25.00")
    assert booking.caution_due == D("0.00")
    assert booking.needs_attention


def test_caution_excess_is_shown_in_the_admin(admin_client):
    booking, variant = paid_booking()
    lifecycle.change_item(booking, variant, quantity=1)

    page = admin_client.get(reverse("admin:bookings_booking_change", args=[booking.pk]))

    text = page.content.decode()
    assert "Caution to refund" in text
    assert "25.00" in text


def test_tax_exemption_sets_tax_to_zero_and_records_who_and_when(staff):
    booking, variant = paid_booking()
    before = timezone.now()

    lifecycle.set_tax_exemption(booking, exempt=True, certificate="TX-01-339", by=staff)

    booking.refresh_from_db()
    assert booking.tax_exempt
    assert booking.tax_exempt_certificate == "TX-01-339"
    assert booking.tax_exempt_by == staff
    assert booking.tax_exempt_at >= before
    assert booking.tax == D("0.00")
    assert booking.total == D("100.00")
    assert booking.overpaid == D("10.00")
    assert booking.needs_attention


def test_tax_exemption_needs_a_certificate(staff):
    booking, _ = paid_booking()

    with pytest.raises(lifecycle.BookingStateError, match="certificate"):
        lifecycle.set_tax_exemption(booking, exempt=True, certificate="  ", by=staff)

    booking.refresh_from_db()
    assert not booking.tax_exempt
    assert booking.tax == D("10.00")


def test_the_database_refuses_an_exemption_without_a_certificate():
    with pytest.raises(IntegrityError), transaction.atomic():
        BookingFactory(tax_exempt=True, tax_exempt_certificate="")


def test_exempt_bookings_stay_tax_free_through_line_changes_and_repricing(staff):
    booking, variant = paid_booking()
    lifecycle.set_tax_exemption(booking, exempt=True, certificate="TX-01-339", by=staff)

    lifecycle.change_item(booking, variant, quantity=3)
    booking.refresh_from_db()
    assert booking.tax == D("0.00")

    quote.reprice_booking(booking)
    booking.refresh_from_db()
    assert booking.tax == D("0.00")


def test_removing_the_exemption_recomputes_the_tax(staff):
    booking, _ = paid_booking()
    lifecycle.set_tax_exemption(booking, exempt=True, certificate="TX-01-339", by=staff)

    lifecycle.set_tax_exemption(booking, exempt=False, certificate="", by=staff)

    booking.refresh_from_db()
    assert not booking.tax_exempt
    assert booking.tax == D("10.00")
    assert booking.tax_exempt_by == staff


def test_tax_exemption_is_refused_while_a_checkout_is_open(staff):
    variant = ItemVariantFactory()
    booking = BookingFactory(
        status=Status.HELD, hold_expires_at=timezone.now() + timedelta(minutes=15)
    )
    BookingItemFactory(booking=booking, variant=variant, quantity=1, unit_price=D("200.00"))
    start_checkout(booking, Payment.Kind.FULL)

    with pytest.raises(lifecycle.BookingStateError, match="in progress"):
        lifecycle.set_tax_exemption(booking, exempt=True, certificate="TX-1", by=staff)


def exemption_url(booking):
    return reverse("admin:bookings_booking_ledger", args=[booking.pk, "tax-exemption"])


def test_setting_an_exemption_in_the_admin_needs_its_permission(client):
    booking, _ = paid_booking()
    user = get_user_model().objects.create_user("clerk", is_staff=True)
    user.user_permissions.set(
        Permission.objects.filter(codename__in=["view_booking", "change_booking"])
    )
    client.force_login(user)
    data = {"tax_exempt": "on", "certificate": "TX-01-339"}

    assert client.get(exemption_url(booking)).status_code == 403
    assert client.post(exemption_url(booking), data).status_code == 403

    user.user_permissions.add(Permission.objects.get(codename="can_set_tax_exemption"))
    response = client.post(exemption_url(booking), data, follow=True)

    assert response.status_code == 200
    booking.refresh_from_db()
    assert booking.tax_exempt
    assert booking.tax_exempt_by == user
    assert booking.tax == D("0.00")


def test_exemption_fields_cannot_be_edited_on_the_booking_form(admin_client):
    booking, _ = paid_booking()
    form = admin_client.get(reverse("admin:bookings_booking_change", args=[booking.pk]))

    fields = form.context["adminform"].form.fields
    assert "tax_exempt" not in fields
    assert "tax_exempt_certificate" not in fields
