"""Stripe Checkout and webhook: money is credited once, only when verified."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
import stripe
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.services import availability
from apps.bookings.tests.factories import BookingItemFactory, ItemVariantFactory
from apps.payments.models import Payment, StripeEvent
from apps.payments.services import stripe as stripe_service
from apps.payments.services.ledger import PaymentError
from apps.payments.tests.helpers import (
    URLS,
    charge_refunded_event,
    event,
    held_booking,
    pay,
    post_event,
    session_object,
    start_checkout,
)

pytestmark = pytest.mark.django_db

D = Decimal


def test_deposit_then_balance_gives_paid_and_zero_balance(client):
    booking = held_booking(total="200.00")

    deposit = pay(client, booking, Payment.Kind.DEPOSIT)
    assert deposit.amount == D("100.00")
    assert deposit.status == Payment.Status.SUCCEEDED
    assert booking.status == Booking.Status.DEPOSIT_PAID
    assert booking.hold_expires_at is None
    assert booking.amount_paid == D("100.00")
    assert booking.balance_due == D("100.00")

    balance = pay(client, booking, Payment.Kind.BALANCE)
    assert balance.amount == D("100.00")
    assert booking.status == Booking.Status.PAID
    assert booking.amount_paid == D("200.00")
    assert booking.balance_due == D("0.00")
    assert not booking.needs_attention


def test_full_payment_gives_paid(client):
    booking = held_booking(total="150.00", delivery_fee=D("25.00"))
    payment = pay(client, booking, Payment.Kind.FULL)
    assert payment.amount == D("175.00")
    assert booking.status == Booking.Status.PAID
    assert booking.balance_due == D("0.00")


def test_duplicate_webhook_event_is_ignored(client):
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.DEPOSIT)
    payload = event("checkout.session.completed", session_object(payment))

    assert post_event(client, payload).status_code == 200
    booking.refresh_from_db()
    payment.refresh_from_db()
    first = (booking.status, booking.amount_paid, booking.updated_at, payment.updated_at)

    assert post_event(client, payload).status_code == 200
    booking.refresh_from_db()
    payment.refresh_from_db()
    assert (booking.status, booking.amount_paid, booking.updated_at, payment.updated_at) == first
    assert booking.amount_paid == D("100.00")
    assert booking.payments.count() == 1
    assert StripeEvent.objects.count() == 1


def test_same_payment_reported_by_two_events_is_credited_once(client):
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.DEPOSIT)
    post_event(client, event("checkout.session.completed", session_object(payment)))
    post_event(client, event("checkout.session.async_payment_succeeded", session_object(payment)))
    booking.refresh_from_db()
    assert booking.amount_paid == D("100.00")
    assert booking.status == Booking.Status.DEPOSIT_PAID


def test_invalid_signature_is_rejected(client):
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    payload = event("checkout.session.completed", session_object(payment))

    assert post_event(client, payload, secret="whsec_wrong").status_code == 400  # noqa: S106
    assert post_event(client, payload, signature="not-a-signature").status_code == 400
    unsigned = client.post(
        reverse("payments:stripe_webhook"), data="{}", content_type="application/json"
    )
    assert unsigned.status_code == 400

    booking.refresh_from_db()
    payment.refresh_from_db()
    assert booking.status == Booking.Status.HELD
    assert booking.amount_paid == D("0.00")
    assert payment.status == Payment.Status.PENDING
    assert not StripeEvent.objects.exists()


def test_webhook_is_rejected_when_no_secret_is_configured(client, settings):
    settings.STRIPE_WEBHOOK_SECRET = ""
    booking = held_booking()
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    payload = event("checkout.session.completed", session_object(payment))
    assert post_event(client, payload, secret="").status_code == 400
    booking.refresh_from_db()
    assert booking.status == Booking.Status.HELD


def test_webhook_only_accepts_post(client):
    assert client.get(reverse("payments:stripe_webhook")).status_code == 405


def test_ach_stays_pending_until_it_clears(client):
    booking = held_booking(total="200.00")
    payment = pay(client, booking, Payment.Kind.DEPOSIT, payment_status="unpaid")

    assert payment.status == Payment.Status.PENDING
    assert payment.method == Payment.Method.ACH
    assert booking.status == Booking.Status.HELD
    assert booking.amount_paid == D("0.00")
    assert booking.hold_expires_at > timezone.now() + timedelta(days=6)

    post_event(client, event("checkout.session.async_payment_succeeded", session_object(payment)))
    booking.refresh_from_db()
    payment.refresh_from_db()
    assert payment.status == Payment.Status.SUCCEEDED
    assert booking.status == Booking.Status.DEPOSIT_PAID
    assert booking.amount_paid == D("100.00")


def test_failed_ach_payment_leaves_booking_unpaid(client):
    booking = held_booking(total="200.00")
    payment = pay(client, booking, Payment.Kind.FULL, payment_status="unpaid")

    response = post_event(
        client,
        event("checkout.session.async_payment_failed", session_object(payment, "unpaid")),
    )
    assert response.status_code == 200
    booking.refresh_from_db()
    payment.refresh_from_db()
    assert payment.status == Payment.Status.FAILED
    assert booking.status == Booking.Status.HELD
    assert booking.amount_paid == D("0.00")
    assert booking.balance_due == D("200.00")
    assert booking.needs_attention


def test_amount_cannot_be_changed_from_the_client_side(client):
    booking = held_booking(total="200.00")

    with pytest.raises(TypeError):
        stripe_service.create_checkout_session(booking, Payment.Kind.FULL, amount=D("1.00"), **URLS)

    payment, create_kwargs = start_checkout(booking, Payment.Kind.FULL)
    assert payment.amount == D("200.00")
    assert [line["price_data"]["unit_amount"] for line in create_kwargs["line_items"]] == [20000]
    assert create_kwargs["payment_method_types"] == ["card", "us_bank_account"]
    assert create_kwargs["metadata"] == {
        "booking_id": str(booking.pk),
        "kind": "full",
        "payment_id": str(payment.pk),
    }

    tampered = session_object(payment, amount_total=100)
    assert post_event(client, event("checkout.session.completed", tampered)).status_code == 200
    booking.refresh_from_db()
    payment.refresh_from_db()
    assert payment.status == Payment.Status.PENDING
    assert booking.status == Booking.Status.HELD
    assert booking.amount_paid == D("0.00")
    assert booking.needs_attention


def test_webhook_metadata_cannot_redirect_a_payment_to_another_booking(client):
    booking = held_booking(total="200.00")
    other = held_booking(total="50.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    session = session_object(payment)
    session["metadata"]["booking_id"] = str(other.pk)

    post_event(client, event("checkout.session.completed", session))
    booking.refresh_from_db()
    other.refresh_from_db()
    assert booking.status == Booking.Status.PAID
    assert other.status == Booking.Status.HELD
    assert other.amount_paid == D("0.00")


def test_caution_fee_is_a_separate_line_and_passes_the_amount_check(client):
    booking = held_booking(total="200.00", caution_fee="75.00")

    deposit, deposit_kwargs = start_checkout(booking, Payment.Kind.DEPOSIT)
    assert len(deposit_kwargs["line_items"]) == 1
    assert deposit.caution_amount == D("0.00")
    post_event(client, event("checkout.session.completed", session_object(deposit)))

    balance, balance_kwargs = start_checkout(booking, Payment.Kind.BALANCE)
    amounts = [line["price_data"]["unit_amount"] for line in balance_kwargs["line_items"]]
    assert amounts == [10000, 7500]
    session = session_object(balance)
    assert session["amount_total"] == 17500

    post_event(client, event("checkout.session.completed", session))
    booking.refresh_from_db()
    balance.refresh_from_db()
    assert balance.status == Payment.Status.SUCCEEDED
    assert booking.status == Booking.Status.PAID
    assert booking.amount_paid == D("200.00")
    assert booking.balance_due == D("0.00")
    assert booking.caution_held == D("75.00")
    assert not booking.needs_attention


def test_caution_fee_on_full_payment_stays_out_of_amount_paid(client):
    booking = held_booking(total="200.00", caution_fee="75.00")
    payment = pay(client, booking, Payment.Kind.FULL)
    assert payment.charged_total == D("275.00")
    assert booking.status == Booking.Status.PAID
    assert booking.amount_paid == D("200.00")
    assert booking.caution_held == D("75.00")


def test_full_refund_cancels_the_booking_and_frees_stock(client, refunds):
    booking = held_booking(total="200.00")
    variant = booking.items.get().variant
    before = availability.available_quantity(variant, booking.event_date)
    payment = pay(client, booking, Payment.Kind.FULL)

    refunds.add(payment, "200.00")
    post_event(client, charge_refunded_event(payment, refunds))
    booking.refresh_from_db()
    payment.refresh_from_db()
    assert payment.status == Payment.Status.REFUNDED
    assert booking.amount_paid == D("0.00")
    assert booking.status == Booking.Status.CANCELLED
    assert availability.available_quantity(variant, booking.event_date) == before + 1


def test_partial_refund_below_deposit_keeps_status_and_flags_attention(client, refunds):
    booking = held_booking(total="200.00")
    payment = pay(client, booking, Payment.Kind.FULL)

    refunds.add(payment, "150.00")
    post_event(client, charge_refunded_event(payment, refunds))
    booking.refresh_from_db()
    payment.refresh_from_db()
    assert payment.status == Payment.Status.SUCCEEDED
    assert booking.amount_paid == D("50.00")
    assert booking.status == Booking.Status.PAID
    assert booking.needs_attention


def test_partial_refund_still_covering_deposit_sets_deposit_paid(client, refunds):
    booking = held_booking(total="200.00")
    payment = pay(client, booking, Payment.Kind.FULL)

    refunds.add(payment, "60.00")
    post_event(client, charge_refunded_event(payment, refunds))
    booking.refresh_from_db()
    assert booking.amount_paid == D("140.00")
    assert booking.balance_due == D("60.00")
    assert booking.status == Booking.Status.DEPOSIT_PAID
    assert not booking.needs_attention


def test_refunding_the_balance_payment_returns_booking_to_deposit_paid(client, refunds):
    booking = held_booking(total="200.00")
    pay(client, booking, Payment.Kind.DEPOSIT)
    balance = pay(client, booking, Payment.Kind.BALANCE)

    refunds.add(balance, "100.00")
    post_event(client, charge_refunded_event(balance, refunds))
    booking.refresh_from_db()
    assert booking.amount_paid == D("100.00")
    assert booking.status == Booking.Status.DEPOSIT_PAID


def test_refund_totals_are_cumulative_so_repeats_do_not_double_count(client, refunds):
    booking = held_booking(total="200.00")
    payment = pay(client, booking, Payment.Kind.FULL)

    refunds.add(payment, "60.00")
    post_event(client, charge_refunded_event(payment, refunds))
    post_event(client, charge_refunded_event(payment, refunds))
    booking.refresh_from_db()
    assert booking.amount_paid == D("140.00")


def test_payment_after_hold_lapsed_is_kept_and_rebooks_when_stock_is_free(client):
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    Booking.objects.filter(pk=booking.pk).update(
        status=Booking.Status.EXPIRED, hold_expires_at=timezone.now() - timedelta(hours=1)
    )

    post_event(client, event("checkout.session.completed", session_object(payment)))
    booking.refresh_from_db()
    assert booking.amount_paid == D("200.00")
    assert booking.status == Booking.Status.PAID
    assert not booking.needs_attention


def test_payment_after_hold_lapsed_and_stock_taken_is_kept_and_flagged(client):
    variant = ItemVariantFactory(stock_quantity=1)
    booking = held_booking(total="0.00", delivery_fee=D("200.00"))
    booking.items.all().delete()
    BookingItemFactory(booking=booking, variant=variant, quantity=1, unit_price=D("0.00"))
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    Booking.objects.filter(pk=booking.pk).update(
        status=Booking.Status.EXPIRED, hold_expires_at=timezone.now() - timedelta(hours=1)
    )
    rival = held_booking(total="10.00")
    rival.items.update(variant=variant)

    post_event(client, event("checkout.session.completed", session_object(payment)))
    booking.refresh_from_db()
    assert booking.amount_paid == D("200.00")
    assert booking.status == Booking.Status.EXPIRED
    assert booking.needs_attention


def test_checkout_extends_the_hold_to_outlast_the_session():
    booking = held_booking()
    _, create_kwargs = start_checkout(booking, Payment.Kind.FULL)
    booking.refresh_from_db()
    assert booking.hold_expires_at.timestamp() > create_kwargs["expires_at"]


def test_checkout_is_refused_when_booking_is_not_open_for_payment():
    booking = held_booking()
    Booking.objects.filter(pk=booking.pk).update(
        hold_expires_at=timezone.now() - timedelta(minutes=1)
    )
    with pytest.raises(PaymentError):
        start_checkout(booking, Payment.Kind.FULL)
    assert not booking.payments.exists()


@pytest.mark.parametrize("kind", [Payment.Kind.CAUTION_REFUND, Payment.Kind.DAMAGE_CHARGE])
def test_checkout_is_refused_for_non_rental_kinds(kind):
    with pytest.raises(PaymentError):
        start_checkout(held_booking(), kind)


def test_balance_checkout_is_refused_once_fully_paid(client):
    booking = held_booking()
    pay(client, booking, Payment.Kind.FULL)
    with pytest.raises(PaymentError):
        start_checkout(booking, Payment.Kind.BALANCE)


def test_new_checkout_expires_the_previous_open_session():
    booking = held_booking()
    first, _ = start_checkout(booking, Payment.Kind.DEPOSIT)
    with mock.patch.object(stripe_service.stripe.checkout, "Session") as session_api:
        session_api.create.return_value = {"id": "cs_second"}
        stripe_service.create_checkout_session(booking, Payment.Kind.DEPOSIT, **URLS)
        assert session_api.expire.call_args.args == (first.stripe_checkout_session_id,)
    first.refresh_from_db()
    assert first.status == Payment.Status.FAILED


def test_new_checkout_is_refused_while_a_payment_is_clearing():
    booking = held_booking()
    start_checkout(booking, Payment.Kind.DEPOSIT)
    with mock.patch.object(stripe_service.stripe.checkout, "Session") as session_api:
        session_api.expire.side_effect = stripe.InvalidRequestError("complete", None)
        session_api.retrieve.return_value = {"status": "complete"}
        with pytest.raises(PaymentError):
            stripe_service.create_checkout_session(booking, Payment.Kind.DEPOSIT, **URLS)
        session_api.create.assert_not_called()
    assert booking.payments.count() == 1


def test_stripe_error_marks_the_payment_failed():
    booking = held_booking()
    with mock.patch.object(stripe_service.stripe.checkout, "Session") as session_api:
        session_api.create.side_effect = stripe.APIConnectionError("down")
        with pytest.raises(stripe.APIConnectionError):
            stripe_service.create_checkout_session(booking, Payment.Kind.FULL, **URLS)
    assert booking.payments.get().status == Payment.Status.FAILED


def test_handler_error_rolls_back_so_the_retry_is_processed(client):
    booking = held_booking(total="200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    payload = event("checkout.session.completed", session_object(payment))

    with mock.patch.object(
        stripe_service.ledger, "record_success", side_effect=RuntimeError("boom")
    ):
        with pytest.raises(RuntimeError):
            post_event(client, payload)
    assert not StripeEvent.objects.exists()

    assert post_event(client, payload).status_code == 200
    booking.refresh_from_db()
    assert booking.status == Booking.Status.PAID


def test_event_for_unknown_session_is_acknowledged_and_ignored(client):
    session = {"id": "cs_unknown", "amount_total": 100, "currency": "usd", "metadata": {}}
    assert post_event(client, event("checkout.session.completed", session)).status_code == 200
    assert not Payment.objects.exists()
