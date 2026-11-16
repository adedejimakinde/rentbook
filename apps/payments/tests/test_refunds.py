"""Refunds issued from the admin through Stripe, and the dashboard-refund safety net."""

from decimal import Decimal
from unittest import mock

import pytest
import stripe
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.bookings.models import Booking
from apps.payments.models import Payment
from apps.payments.services import ledger
from apps.payments.services import stripe as stripe_service
from apps.payments.tests.helpers import (
    charge_refunded_event,
    event,
    held_booking,
    pay,
    post_event,
)

pytestmark = pytest.mark.django_db

D = Decimal
CAUTION = Payment.Kind.CAUTION_REFUND
RENTAL = stripe_service.RENTAL_REFUND


@pytest.fixture
def staff():
    return get_user_model().objects.create_superuser("owner", "owner@example.com", "pw")


@pytest.fixture
def paid_with_caution(client):
    """A booking paid in full by card: $200 rental plus $75 caution in one charge."""
    booking = held_booking(total="200.00", caution_fee="75.00")
    payment = pay(client, booking, Payment.Kind.FULL)
    return booking, payment


def ledger_url(booking, action):
    return reverse("admin:bookings_booking_ledger", args=[booking.pk, action])


def test_caution_refund_goes_through_stripe_tagged_as_caution(paid_with_caution, refunds, staff):
    booking, payment = paid_with_caution

    stripe_service.refund_caution(booking, D("75.00"), received_by=staff)

    (call,) = refunds.create_calls
    assert call["payment_intent"] == payment.stripe_payment_intent_id
    assert call["amount"] == 7500
    assert call["metadata"] == {
        "kind": "caution_refund",
        "booking_id": str(booking.pk),
        "payment_id": str(payment.pk),
    }
    booking.refresh_from_db()
    payment.refresh_from_db()
    entry = booking.payments.get(kind=CAUTION)
    assert entry.amount == D("75.00")
    assert entry.stripe_refund_id == refunds.refunds[0]["id"]
    assert (entry.refund_of, entry.received_by, entry.method) == (payment, staff, payment.method)
    assert booking.caution_held == D("0.00")
    assert payment.refunded_amount == D("0.00")
    assert booking.amount_paid == D("200.00")
    assert booking.status == Booking.Status.PAID
    assert not booking.needs_attention


def test_webhook_after_app_caution_refund_changes_nothing(client, paid_with_caution, refunds):
    booking, payment = paid_with_caution
    stripe_service.refund_caution(booking, D("75.00"))

    post_event(client, charge_refunded_event(payment, refunds))
    post_event(client, charge_refunded_event(payment, refunds))

    booking.refresh_from_db()
    assert booking.payments.filter(kind=CAUTION).count() == 1
    assert booking.caution_held == D("0.00")
    assert booking.amount_paid == D("200.00")
    assert booking.status == Booking.Status.PAID
    assert not booking.needs_attention


def test_webhook_alone_records_a_tagged_caution_refund(client, paid_with_caution, refunds):
    """If the app dies right after Stripe accepts the refund, the webhook still records it."""
    booking, payment = paid_with_caution
    refunds.add(payment, "50.00", kind=CAUTION)

    post_event(client, charge_refunded_event(payment, refunds))

    booking.refresh_from_db()
    assert booking.payments.get(kind=CAUTION).amount == D("50.00")
    assert booking.caution_held == D("25.00")
    assert booking.amount_paid == D("200.00")
    assert booking.status == Booking.Status.PAID
    assert not booking.needs_attention


def test_app_refund_is_still_recorded_when_the_follow_up_sync_fails(
    client, paid_with_caution, refunds
):
    booking, payment = paid_with_caution
    with mock.patch.object(refunds, "list", side_effect=stripe.APIConnectionError("down")):
        stripe_service.refund_caution(booking, D("75.00"))
    assert not booking.payments.filter(kind=CAUTION).exists()

    post_event(client, charge_refunded_event(payment, refunds))
    assert booking.payments.get(kind=CAUTION).amount == D("75.00")


def test_partial_caution_refunds_add_up_and_cannot_exceed_what_is_held(
    paid_with_caution, refunds, staff
):
    booking, _ = paid_with_caution
    ledger.record_damage_charge(
        booking, amount=D("25.00"), reference="Stain", received_at=None, received_by=staff
    )
    stripe_service.refund_caution(booking, D("30.00"))
    with pytest.raises(ledger.PaymentError):
        stripe_service.refund_caution(booking, D("20.01"))
    stripe_service.refund_caution(booking, D("20.00"))

    assert len(refunds.create_calls) == 2
    keys = {call["idempotency_key"] for call in refunds.create_calls}
    assert len(keys) == 2
    assert booking.caution_held == D("0.00")
    assert booking.amount_paid == D("200.00")


def test_caution_refund_through_stripe_needs_a_stripe_payment_holding_it(refunds, staff):
    booking = held_booking(total="200.00", caution_fee="75.00")
    ledger.record_manual_payment(
        booking,
        method=Payment.Method.ZELLE,
        amount=D("200.00"),
        caution_amount=D("75.00"),
        received_at=None,
        reference="ZL-1",
        received_by=staff,
    )
    with pytest.raises(ledger.PaymentError):
        stripe_service.refund_caution(booking, D("75.00"))
    assert refunds.create_calls == []
    assert booking.caution_held == D("75.00")


def test_rental_refund_through_stripe_is_tagged_and_not_flagged(paid_with_caution, refunds, staff):
    booking, payment = paid_with_caution

    stripe_service.refund_rental(payment, D("60.00"), received_by=staff)

    (call,) = refunds.create_calls
    assert (call["amount"], call["metadata"]["kind"]) == (6000, RENTAL)
    booking.refresh_from_db()
    payment.refresh_from_db()
    assert payment.refunded_amount == D("60.00")
    assert booking.amount_paid == D("140.00")
    assert booking.status == Booking.Status.DEPOSIT_PAID
    assert booking.caution_held == D("75.00")
    assert not booking.payments.filter(kind=CAUTION).exists()
    assert not booking.needs_attention


def test_full_rental_refund_cancels_and_leaves_caution_to_refund_separately(
    client, paid_with_caution, refunds
):
    booking, payment = paid_with_caution

    stripe_service.refund_rental(payment, D("200.00"))
    booking.refresh_from_db()
    payment.refresh_from_db()
    assert payment.status == Payment.Status.REFUNDED
    assert booking.status == Booking.Status.CANCELLED
    assert booking.amount_paid == D("0.00")
    assert booking.caution_held == D("75.00")

    stripe_service.refund_caution(booking, D("75.00"))
    post_event(client, charge_refunded_event(payment, refunds))
    booking.refresh_from_db()
    payment.refresh_from_db()
    assert booking.caution_held == D("0.00")
    assert payment.refunded_amount == D("200.00")
    assert booking.status == Booking.Status.CANCELLED


def test_rental_refund_cannot_exceed_the_rental_money_on_the_payment(paid_with_caution, refunds):
    booking, payment = paid_with_caution
    with pytest.raises(ledger.PaymentError):
        stripe_service.refund_rental(payment, D("200.01"))
    stripe_service.refund_rental(payment, D("150.00"))
    with pytest.raises(ledger.PaymentError):
        stripe_service.refund_rental(payment, D("50.01"))
    assert len(refunds.create_calls) == 1
    assert booking.amount_paid == D("50.00")


def test_rental_refund_is_refused_for_manual_and_unpaid_payments(client, refunds, staff):
    booking = held_booking(total="200.00")
    manual = ledger.record_manual_payment(
        booking,
        method=Payment.Method.ZELLE,
        amount=D("100.00"),
        received_at=None,
        reference="ZL-1",
        received_by=staff,
    )
    pending = pay(client, booking, Payment.Kind.BALANCE, payment_status="unpaid")
    for payment in (manual, pending):
        with pytest.raises(ledger.PaymentError):
            stripe_service.refund_rental(payment, D("10.00"))
    assert refunds.create_calls == []


def test_stripe_refusing_the_refund_changes_nothing(paid_with_caution, refunds):
    booking, payment = paid_with_caution
    with mock.patch.object(refunds, "create", side_effect=stripe.InvalidRequestError("no", None)):
        with pytest.raises(stripe.InvalidRequestError):
            stripe_service.refund_rental(payment, D("60.00"))
    booking.refresh_from_db()
    assert booking.amount_paid == D("200.00")
    assert booking.status == Booking.Status.PAID


def test_dashboard_refund_on_a_charge_with_caution_counts_as_rental_and_is_flagged(
    client, paid_with_caution, refunds
):
    """Safety net: an untagged refund is ambiguous, so rental first and flag for staff."""
    booking, payment = paid_with_caution
    refunds.add(payment, "75.00")

    post_event(client, charge_refunded_event(payment, refunds))

    booking.refresh_from_db()
    assert booking.amount_paid == D("125.00")
    assert booking.caution_held == D("75.00")
    assert booking.status == Booking.Status.DEPOSIT_PAID
    assert booking.needs_attention


def test_dashboard_and_app_refunds_on_one_charge_are_kept_apart(client, paid_with_caution, refunds):
    booking, payment = paid_with_caution
    stripe_service.refund_caution(booking, D("75.00"))
    stripe_service.refund_rental(payment, D("20.00"))
    refunds.add(payment, "30.00")

    post_event(client, charge_refunded_event(payment, refunds))

    booking.refresh_from_db()
    assert booking.amount_paid == D("150.00")
    assert booking.caution_held == D("0.00")
    assert booking.payments.filter(kind=CAUTION).count() == 1
    assert booking.needs_attention


def test_refund_reported_by_webhook_but_missing_from_the_list_still_counts(
    client, paid_with_caution, refunds
):
    booking, payment = paid_with_caution
    refunded = charge_refunded_event(payment, refunds)
    refunded["data"]["object"]["amount_refunded"] = 4000

    post_event(client, refunded)

    booking.refresh_from_db()
    assert booking.amount_paid == D("160.00")
    assert booking.needs_attention


def test_failed_and_cancelled_stripe_refunds_are_not_counted(client, paid_with_caution, refunds):
    booking, payment = paid_with_caution
    refunds.add(payment, "200.00", kind=RENTAL, status="failed")
    refunds.add(payment, "75.00", kind=CAUTION, status="canceled")

    post_event(client, charge_refunded_event(payment, refunds))

    booking.refresh_from_db()
    assert booking.amount_paid == D("200.00")
    assert booking.caution_held == D("75.00")
    assert booking.status == Booking.Status.PAID


def test_refund_failed_event_flags_the_booking(client, paid_with_caution, refunds):
    booking, payment = paid_with_caution
    stripe_service.refund_rental(payment, D("60.00"))
    failed = {**refunds.refunds[0], "status": "failed"}

    assert post_event(client, event("refund.failed", failed)).status_code == 200
    booking.refresh_from_db()
    assert booking.needs_attention


def test_admin_caution_refund_by_stripe_issues_the_refund(
    client, staff, paid_with_caution, refunds
):
    client.force_login(staff)
    booking, _ = paid_with_caution

    response = client.post(
        ledger_url(booking, "caution-refund"),
        {"method": "stripe", "amount": "75.00", "date": "2026-11-10"},
    )

    assert response.status_code == 302
    assert refunds.create_calls[0]["metadata"]["kind"] == "caution_refund"
    entry = booking.payments.get(kind=CAUTION)
    assert entry.received_by == staff
    assert booking.caution_held == D("0.00")
    assert booking.amount_paid == D("200.00")


def test_admin_manual_caution_refund_does_not_touch_stripe(
    client, staff, paid_with_caution, refunds
):
    client.force_login(staff)
    booking, _ = paid_with_caution

    missing_reference = client.post(
        ledger_url(booking, "caution-refund"),
        {"method": "zelle", "amount": "75.00", "date": "2026-11-10"},
    )
    assert missing_reference.status_code == 200
    assert not booking.payments.filter(kind=CAUTION).exists()

    response = client.post(
        ledger_url(booking, "caution-refund"),
        {"method": "zelle", "amount": "75.00", "date": "2026-11-10", "reference": "ZL-77"},
    )
    assert response.status_code == 302
    assert refunds.create_calls == []
    entry = booking.payments.get(kind=CAUTION)
    assert (entry.method, entry.reference, entry.stripe_refund_id) == ("zelle", "ZL-77", None)
    assert booking.caution_held == D("0.00")


def test_admin_refund_rental_payment_action(client, staff, paid_with_caution, refunds):
    client.force_login(staff)
    booking, payment = paid_with_caution

    response = client.post(
        reverse("admin:bookings_booking_changelist"),
        {"action": "refund_rental_payment", "_selected_action": [booking.pk]},
    )
    assert response["Location"] == ledger_url(booking, "rental-refund")
    assert client.get(response["Location"]).status_code == 200

    response = client.post(
        ledger_url(booking, "rental-refund"), {"payment": payment.pk, "amount": "200.00"}
    )
    assert response.status_code == 302
    booking.refresh_from_db()
    assert booking.amount_paid == D("0.00")
    assert booking.status == Booking.Status.CANCELLED
    assert booking.caution_held == D("75.00")


def test_admin_rental_refund_shows_errors_and_only_offers_this_bookings_payments(
    client, staff, paid_with_caution, refunds
):
    client.force_login(staff)
    booking, payment = paid_with_caution
    other = held_booking(total="50.00")
    other_payment = pay(client, other, Payment.Kind.FULL)

    too_much = client.post(
        ledger_url(booking, "rental-refund"), {"payment": payment.pk, "amount": "500.00"}
    )
    assert too_much.status_code == 200
    assert b"more than the rental money left" in too_much.content

    wrong_booking = client.post(
        ledger_url(booking, "rental-refund"), {"payment": other_payment.pk, "amount": "10.00"}
    )
    assert wrong_booking.status_code == 200

    with mock.patch.object(refunds, "create", side_effect=stripe.APIConnectionError("down")):
        stripe_down = client.post(
            ledger_url(booking, "rental-refund"), {"payment": payment.pk, "amount": "10.00"}
        )
    assert stripe_down.status_code == 200
    assert b"Stripe could not do this" in stripe_down.content

    assert refunds.create_calls == []
    other.refresh_from_db()
    assert other.amount_paid == D("50.00")
    assert booking.amount_paid == D("200.00")
