"""Manual payments and caution deposit outcomes recorded by staff."""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking
from apps.payments.models import Payment
from apps.payments.services import ledger
from apps.payments.tests.helpers import held_booking, pay

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture
def staff():
    return get_user_model().objects.create_superuser("owner", "owner@example.com", "pw")


def manual(booking, amount, staff, **kwargs):
    return ledger.record_manual_payment(
        booking,
        method=kwargs.pop("method", Payment.Method.ZELLE),
        amount=D(amount),
        received_at=timezone.now(),
        reference="ZL-123",
        received_by=staff,
        **kwargs,
    )


def test_manual_payment_updates_the_booking_correctly(staff):
    booking = held_booking(total="200.00")

    deposit = manual(booking, "100.00", staff)
    booking.refresh_from_db()
    assert deposit.kind == Payment.Kind.DEPOSIT
    assert deposit.status == Payment.Status.SUCCEEDED
    assert (deposit.reference, deposit.received_by) == ("ZL-123", staff)
    assert booking.status == Booking.Status.DEPOSIT_PAID
    assert booking.hold_expires_at is None
    assert booking.balance_due == D("100.00")

    balance = manual(booking, "100.00", staff, method=Payment.Method.CASHAPP)
    booking.refresh_from_db()
    assert balance.kind == Payment.Kind.BALANCE
    assert booking.status == Booking.Status.PAID
    assert booking.amount_paid == D("200.00")
    assert booking.balance_due == D("0.00")


def test_manual_full_payment_gives_paid(staff):
    booking = held_booking(total="200.00")
    payment = manual(booking, "200.00", staff, method=Payment.Method.BANK_DEPOSIT)
    booking.refresh_from_db()
    assert payment.kind == Payment.Kind.FULL
    assert booking.status == Booking.Status.PAID


def test_manual_payment_below_deposit_is_kept_and_flagged(staff):
    booking = held_booking(total="200.00")
    manual(booking, "40.00", staff)
    booking.refresh_from_db()
    assert booking.status == Booking.Status.HELD
    assert booking.amount_paid == D("40.00")
    assert booking.needs_attention


def test_manual_overpayment_is_rejected(staff):
    booking = held_booking(total="200.00")
    with pytest.raises(ledger.PaymentError):
        manual(booking, "200.01", staff)
    booking.refresh_from_db()
    assert booking.status == Booking.Status.HELD
    assert not booking.payments.exists()


@pytest.mark.parametrize("amount", ["0.00", "-5.00", "10.001"])
def test_manual_payment_rejects_bad_amounts(staff, amount):
    with pytest.raises(ledger.PaymentError):
        manual(held_booking(), amount, staff)


def test_manual_payment_rejects_stripe_methods(staff):
    with pytest.raises(ledger.PaymentError):
        manual(held_booking(), "50.00", staff, method=Payment.Method.CARD)


def test_manual_payment_on_cancelled_booking_is_rejected(staff):
    booking = held_booking()
    Booking.objects.filter(pk=booking.pk).update(status=Booking.Status.CANCELLED)
    with pytest.raises(ledger.PaymentError):
        manual(booking, "50.00", staff)


def test_manual_and_stripe_payments_share_one_ledger(client, staff):
    booking = held_booking(total="200.00")
    manual(booking, "100.00", staff)
    payment = pay(client, booking, Payment.Kind.BALANCE)
    assert payment.amount == D("100.00")
    assert booking.status == Booking.Status.PAID
    assert booking.balance_due == D("0.00")


def test_caution_refund_and_damage_charge_leave_amount_paid_untouched(staff):
    booking = held_booking(total="200.00", caution_fee="100.00")
    manual(booking, "200.00", staff, caution_amount=D("100.00"))
    booking.refresh_from_db()
    assert booking.amount_paid == D("200.00")
    assert booking.caution_held == D("100.00")

    charge = ledger.record_damage_charge(
        booking,
        amount=D("30.00"),
        reference="Torn linen",
        received_at=timezone.now(),
        received_by=staff,
    )
    refund = ledger.record_caution_refund(
        booking,
        amount=D("70.00"),
        method=Payment.Method.ZELLE,
        reference="ZL-999",
        received_at=timezone.now(),
        received_by=staff,
    )
    booking.refresh_from_db()
    assert charge.kind == Payment.Kind.DAMAGE_CHARGE
    assert refund.kind == Payment.Kind.CAUTION_REFUND
    assert booking.caution_held == D("0.00")
    assert booking.amount_paid == D("200.00")
    assert booking.status == Booking.Status.PAID


def test_caution_cannot_be_released_beyond_what_is_held(staff):
    booking = held_booking(total="200.00", caution_fee="100.00")
    manual(booking, "200.00", staff, caution_amount=D("100.00"))
    kwargs = {"reference": "x", "received_at": timezone.now(), "received_by": staff}
    ledger.record_damage_charge(booking, amount=D("60.00"), **kwargs)
    with pytest.raises(ledger.PaymentError):
        ledger.record_caution_refund(
            booking, amount=D("40.01"), method=Payment.Method.ZELLE, **kwargs
        )
    assert booking.caution_held == D("40.00")


def test_caution_collected_cannot_exceed_the_caution_fee(staff):
    booking = held_booking(total="200.00", caution_fee="100.00")
    with pytest.raises(ledger.PaymentError):
        manual(booking, "200.00", staff, caution_amount=D("100.01"))


def test_deposit_rounds_to_the_cent(settings):
    settings.DEPOSIT_PERCENT = 30
    assert held_booking(total="33.35").deposit_amount == D("10.01")


def ledger_url(booking, action):
    return reverse("admin:bookings_booking_ledger", args=[booking.pk, action])


def test_admin_action_opens_the_form_and_records_a_manual_payment(client, staff):
    client.force_login(staff)
    booking = held_booking(total="200.00")

    response = client.post(
        reverse("admin:bookings_booking_changelist"),
        {"action": "record_manual_payment", "_selected_action": [booking.pk]},
    )
    assert response.status_code == 302
    assert response["Location"] == ledger_url(booking, "manual-payment")
    assert client.get(response["Location"]).status_code == 200

    response = client.post(
        ledger_url(booking, "manual-payment"),
        {
            "method": "zelle",
            "amount": "100.00",
            "caution_amount": "0",
            "date": "2026-11-10",
            "reference": "ZL-555",
        },
    )
    assert response.status_code == 302
    booking.refresh_from_db()
    payment = booking.payments.get()
    assert payment.received_by == staff
    assert timezone.localtime(payment.received_at).date().isoformat() == "2026-11-10"
    assert booking.status == Booking.Status.DEPOSIT_PAID


def test_admin_form_shows_ledger_errors_instead_of_saving(client, staff):
    client.force_login(staff)
    booking = held_booking(total="200.00")
    response = client.post(
        ledger_url(booking, "manual-payment"),
        {"method": "zelle", "amount": "500.00", "date": "2026-11-10", "reference": "ZL-1"},
    )
    assert response.status_code == 200
    assert b"more than the balance due" in response.content
    assert not booking.payments.exists()


def test_admin_records_caution_refund_and_damage_charge(client, staff):
    client.force_login(staff)
    booking = held_booking(total="200.00", caution_fee="100.00")
    manual(booking, "200.00", staff, caution_amount=D("100.00"))

    client.post(
        ledger_url(booking, "damage-charge"),
        {"amount": "25.00", "date": "2026-11-10", "reference": "Broken chair"},
    )
    client.post(
        ledger_url(booking, "caution-refund"),
        {"method": "zelle", "amount": "75.00", "date": "2026-11-11", "reference": "ZL-9"},
    )
    booking.refresh_from_db()
    assert booking.caution_held == D("0.00")
    assert booking.amount_paid == D("200.00")


def test_ledger_forms_require_staff(client):
    booking = held_booking()
    response = client.post(
        ledger_url(booking, "manual-payment"),
        {"method": "zelle", "amount": "100.00", "date": "2026-11-10", "reference": "x"},
    )
    assert response.status_code == 302
    assert not booking.payments.exists()


def test_booking_admin_pages_render_with_money_fields(client, staff):
    client.force_login(staff)
    booking = held_booking(total="200.00")
    assert client.get(reverse("admin:bookings_booking_add")).status_code == 200
    assert client.get(reverse("admin:bookings_booking_changelist")).status_code == 200
    response = client.get(reverse("admin:bookings_booking_change", args=[booking.pk]))
    assert response.status_code == 200
    assert client.get(reverse("admin:payments_payment_changelist")).status_code == 200
