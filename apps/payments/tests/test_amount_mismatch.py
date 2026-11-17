"""A checkout Stripe charged a different amount for can be resolved by staff."""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse

from apps.bookings.models import Booking
from apps.bookings.services import lifecycle
from apps.payments.models import Payment
from apps.payments.services import ledger
from apps.payments.services import stripe as stripe_service
from apps.payments.tests.helpers import (
    URLS,
    event,
    held_booking,
    post_event,
    session_object,
    start_checkout,
    stripe_sessions,
)

pytestmark = pytest.mark.django_db

D = Decimal
Status = Booking.Status
ACTION = "resolve-mismatch"


def charged_differently(client, booking, kind, charged_cents, payment_status="paid"):
    """A checkout Stripe completed for charged_cents, not what was asked."""
    payment, _ = start_checkout(booking, kind)
    session = stripe_sessions().complete(payment.stripe_checkout_session_id, payment_status)
    session["amount_total"] = charged_cents
    data = session_object(payment, payment_status, amount_total=charged_cents)
    assert post_event(client, event("checkout.session.completed", data)).status_code == 200
    payment.refresh_from_db()
    booking.refresh_from_db()
    return payment


@pytest.fixture
def mismatched(client):
    """A $200 booking (with $50 caution) whose $250 full checkout Stripe charged $230 for."""
    booking = held_booking(total="200.00", caution_fee="50.00")
    payment = charged_differently(client, booking, Payment.Kind.FULL, 23000)
    return booking, payment


def ledger_url(booking):
    return reverse("admin:bookings_booking_ledger", args=[booking.pk, ACTION])


def resolve(client, booking, payment, resolution):
    return client.post(
        ledger_url(booking), {"payment": payment.pk, "resolution": resolution}, follow=True
    )


def test_a_mismatched_checkout_is_left_pending_and_flagged_once(mismatched):
    booking, payment = mismatched
    assert payment.status == Payment.Status.PENDING
    assert payment.mismatch_flagged_at is not None
    assert booking.needs_attention


def test_the_sync_does_not_flag_it_again(mismatched, refunds):
    booking, payment = mismatched
    Booking.objects.filter(pk=booking.pk).update(needs_attention=False)

    stripe_service.sync_booking(booking)

    booking.refresh_from_db()
    assert not booking.needs_attention


def test_it_blocks_everything_until_resolved(mismatched):
    booking, payment = mismatched
    with pytest.raises(ledger.PaymentError):
        stripe_service.create_checkout_session(booking, Payment.Kind.FULL, **URLS)
    with pytest.raises(ledger.PaymentError, match="Resolve mismatched payment"):
        stripe_service.close_open_checkouts(booking)
    with pytest.raises(lifecycle.BookingStateError):
        lifecycle.cancel_booking(booking)


def test_accepting_records_what_stripe_charged_and_unblocks_the_booking(
    admin_client, admin_user, mismatched
):
    booking, payment = mismatched

    response = resolve(admin_client, booking, payment, "accept")

    assert response.status_code == 200
    payment.refresh_from_db()
    booking.refresh_from_db()
    assert payment.status == Payment.Status.SUCCEEDED
    assert payment.caution_amount == D("50.00")
    assert payment.amount == D("180.00")
    assert payment.received_by == admin_user
    assert booking.amount_paid == D("180.00")
    assert booking.status == Status.DEPOSIT_PAID
    assert booking.needs_attention
    assert not booking.payments.in_progress().exists()
    lifecycle.change_item(booking, booking.items.get().variant, quantity=1)
    lifecycle.cancel_booking(booking)


def test_accepting_an_overcharge_flags_it_and_shows_the_excess_as_refundable(client, admin_client):
    booking = held_booking(total="200.00")
    payment = charged_differently(client, booking, Payment.Kind.FULL, 26000)

    resolve(admin_client, booking, payment, "accept")

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert payment.amount == D("260.00")
    assert booking.status == Status.PAID
    assert booking.needs_attention
    assert booking.overpaid == D("60.00")
    page = admin_client.get(reverse("admin:bookings_booking_change", args=[booking.pk]))
    assert "60.00" in page.content.decode()
    refund_form = admin_client.get(
        reverse("admin:bookings_booking_ledger", args=[booking.pk, "rental-refund"])
    )
    assert "refundable $260.00" in refund_form.content.decode()


def test_refunding_fails_the_payment_and_refunds_all_of_it_at_stripe(
    admin_client, mismatched, refunds
):
    booking, payment = mismatched

    response = resolve(admin_client, booking, payment, "refund")

    assert response.status_code == 200
    payment.refresh_from_db()
    booking.refresh_from_db()
    assert payment.status == Payment.Status.FAILED
    assert payment.failure_reason == "amount_mismatch"
    [call] = refunds.create_calls
    assert call["payment_intent"] == f"pi_{payment.stripe_checkout_session_id}"
    assert call["amount"] == 23000
    assert payment.reference == refunds.refunds[0]["id"]
    assert booking.amount_paid == D("0.00")
    assert booking.needs_attention

    session = stripe_service.create_checkout_session(booking, Payment.Kind.FULL, **URLS)
    assert session["status"] == "open"


def test_a_refunded_mismatch_is_not_revived_by_its_refund_webhooks(
    client, admin_client, mismatched, refunds
):
    booking, payment = mismatched
    resolve(admin_client, booking, payment, "refund")
    payment.refresh_from_db()
    charge = {
        "id": "ch_mismatch",
        "object": "charge",
        "payment_intent": payment.stripe_payment_intent_id,
        "amount": 23000,
        "amount_refunded": 23000,
    }

    assert post_event(client, event("charge.refunded", charge)).status_code == 200
    stripe_service.sync_booking(booking)

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert payment.status == Payment.Status.FAILED
    assert booking.amount_paid == D("0.00")


def test_after_a_refund_a_manual_payment_can_be_recorded(admin_client, mismatched, refunds):
    booking, payment = mismatched
    resolve(admin_client, booking, payment, "refund")

    response = admin_client.post(
        reverse("admin:bookings_booking_ledger", args=[booking.pk, "manual-payment"]),
        {"method": "zelle", "amount": "100.00", "date": "2026-11-17", "reference": "Z-1"},
        follow=True,
    )

    assert "Record manual payment: saved." in response.content.decode()
    booking.refresh_from_db()
    assert booking.amount_paid == D("100.00")


def test_a_mismatch_still_clearing_can_be_accepted_but_not_refunded(client, admin_client):
    booking = held_booking(total="200.00")
    payment = charged_differently(client, booking, Payment.Kind.FULL, 19000, "unpaid")

    refused = resolve(admin_client, booking, payment, "refund")
    assert "still clearing" in refused.content.decode()
    payment.refresh_from_db()
    assert payment.status == Payment.Status.PENDING

    resolve(admin_client, booking, payment, "accept")

    payment.refresh_from_db()
    assert payment.status == Payment.Status.PENDING
    assert payment.method == Payment.Method.ACH
    assert payment.amount == D("190.00")
    assert payment.mismatch_flagged_at is None


def test_resolving_needs_the_refund_permission(client, mismatched):
    booking, payment = mismatched
    user = get_user_model().objects.create_user("clerk", is_staff=True)
    user.user_permissions.set(
        Permission.objects.filter(codename__in=["view_booking", "change_booking"])
    )
    client.force_login(user)

    assert client.get(ledger_url(booking)).status_code == 403

    user.user_permissions.add(Permission.objects.get(codename="can_issue_refunds"))
    assert client.get(ledger_url(booking)).status_code == 200


def test_only_mismatched_payments_can_be_resolved(admin_client):
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)

    response = resolve(admin_client, booking, payment, "accept")

    payment.refresh_from_db()
    assert payment.status == Payment.Status.PENDING
    assert "Select a valid choice" in response.content.decode()
