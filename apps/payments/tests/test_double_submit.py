"""A manual payment or refund cannot be recorded twice, and admin forms submit once."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.tests.helpers import booking_worth
from apps.payments.models import Payment
from apps.payments.services import ledger

pytestmark = pytest.mark.django_db

D = Decimal
ZELLE = Payment.Method.ZELLE
CASHAPP = Payment.Method.CASHAPP


@pytest.fixture
def staff():
    return get_user_model().objects.create_user("owner", is_staff=True)


@pytest.fixture
def booking():
    return booking_worth(
        "200.00",
        Booking.Status.HELD,
        caution_fee="50.00",
        hold_expires_at=timezone.now() + timedelta(hours=1),
    )


def manual(booking, staff, amount="50.00", method=ZELLE, reference="ZL-100", caution="0.00"):
    return ledger.record_manual_payment(
        booking,
        method=method,
        amount=D(amount),
        caution_amount=D(caution),
        received_at=timezone.now(),
        reference=reference,
        received_by=staff,
    )


def test_same_method_and_reference_is_refused_on_the_same_booking(booking, staff):
    manual(booking, staff)

    with pytest.raises(ledger.PaymentError, match="already recorded"):
        manual(booking, staff)
    with pytest.raises(ledger.PaymentError, match="already recorded"):
        manual(booking, staff, reference=" zl-100 ")

    assert booking.payments.count() == 1


def test_a_different_method_or_reference_is_accepted(booking, staff):
    manual(booking, staff)
    manual(booking, staff, method=CASHAPP)
    manual(booking, staff, reference="ZL-101")
    assert booking.payments.count() == 3


def test_the_same_reference_on_another_booking_is_accepted(booking, staff):
    other = booking_worth("200.00", Booking.Status.HELD, hold_expires_at=booking.hold_expires_at)
    manual(booking, staff)
    manual(other, staff)
    assert Payment.objects.count() == 2


def test_manual_rental_refund_cannot_be_recorded_twice(booking, staff):
    payment = manual(booking, staff, amount="200.00")

    def refund():
        return ledger.record_manual_rental_refund(
            payment,
            amount=D("20.00"),
            method=ZELLE,
            reference="ZL-R1",
            received_at=timezone.now(),
            received_by=staff,
        )

    refund()
    with pytest.raises(ledger.PaymentError, match="already recorded"):
        refund()
    payment.refresh_from_db()
    assert payment.refunded_amount == D("20.00")


def test_manual_caution_refund_cannot_be_recorded_twice(booking, staff):
    manual(booking, staff, amount="200.00", caution="50.00")

    def refund():
        return ledger.record_caution_refund(
            booking,
            amount=D("10.00"),
            method=CASHAPP,
            reference="CA-1",
            received_at=timezone.now(),
            received_by=staff,
        )

    refund()
    with pytest.raises(ledger.PaymentError, match="already recorded"):
        refund()
    assert booking.caution_held == D("40.00")


def test_admin_form_posted_twice_records_once(admin_client, booking):
    url = reverse("admin:bookings_booking_ledger", args=[booking.pk, "manual-payment"])
    data = {
        "method": "zelle",
        "amount": "50.00",
        "caution_amount": "0",
        "date": timezone.localdate().isoformat(),
        "reference": "ZL-9",
    }

    assert admin_client.post(url, data).status_code == 302
    second = admin_client.post(url, data)

    assert second.status_code == 200
    assert "already recorded" in second.content.decode()
    assert booking.payments.count() == 1


def test_submit_once_script_is_on_admin_forms(admin_client, booking):
    ledger_page = admin_client.get(
        reverse("admin:bookings_booking_ledger", args=[booking.pk, "manual-payment"])
    )
    change_page = admin_client.get(reverse("admin:bookings_booking_change", args=[booking.pk]))
    add_page = admin_client.get(reverse("admin:inventory_stockadjustment_add"))

    for page in (ledger_page, change_page, add_page):
        assert page.status_code == 200
        assert "core/js/submit_once.js" in page.content.decode()
