"""Staff can cancel or change lines while an ACH payment clears, with a recorded override."""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.services import lifecycle
from apps.bookings.tests.factories import ItemVariantFactory
from apps.payments.models import Payment
from apps.payments.services import stripe as stripe_service
from apps.payments.tests.helpers import held_booking, pay, start_checkout

pytestmark = pytest.mark.django_db

D = Decimal
Status = Booking.Status
NOTE = "Customer called to cancel; ACH will be refunded when it clears."


@pytest.fixture
def staff():
    return get_user_model().objects.create_user("owner", is_staff=True)


@pytest.fixture
def clearing(client):
    """A $200 booking whose full ACH payment is still clearing."""
    booking = held_booking(total="200.00")
    payment = pay(client, booking, Payment.Kind.FULL, payment_status="unpaid")
    assert payment.status == Payment.Status.PENDING
    assert payment.method == Payment.Method.ACH
    Booking.objects.filter(pk=booking.pk).update(needs_attention=False)
    booking.refresh_from_db()
    return booking, payment


def test_cancel_while_ach_clears_still_needs_an_override_note(clearing, staff):
    booking, _ = clearing
    with pytest.raises(lifecycle.BookingStateError, match="override note"):
        lifecycle.cancel_booking(booking, by=staff)


def test_cancel_with_a_note_goes_ahead_flags_and_records_who_and_when(clearing, staff):
    booking, payment = clearing
    before = timezone.now()

    lifecycle.cancel_booking(booking, by=staff, override_note=NOTE)

    booking.refresh_from_db()
    assert booking.status == Status.CANCELLED
    assert booking.needs_attention
    assert NOTE in booking.clearing_override_note
    assert "Cancelled" in booking.clearing_override_note
    assert booking.clearing_override_by == staff
    assert booking.clearing_override_at >= before
    payment.refresh_from_db()
    assert payment.status == Payment.Status.PENDING


def test_ach_that_clears_after_the_override_cancel_is_kept_and_flagged(
    clearing, staff, intents, refunds
):
    booking, payment = clearing
    lifecycle.cancel_booking(booking, by=staff, override_note=NOTE)
    Booking.objects.filter(pk=booking.pk).update(needs_attention=False)
    intents.statuses[payment.stripe_payment_intent_id] = "succeeded"

    stripe_service.sync_booking(booking)

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert payment.status == Payment.Status.SUCCEEDED
    assert booking.status == Status.CANCELLED
    assert booking.needs_attention


def test_line_change_while_ach_clears_needs_the_note(clearing, staff):
    booking, _ = clearing
    variant = ItemVariantFactory()

    with pytest.raises(lifecycle.BookingStateError, match="override note"):
        lifecycle.change_item(booking, variant, quantity=1, by=staff)

    lifecycle.change_item(booking, variant, quantity=1, by=staff, override_note="Added a table")

    booking.refresh_from_db()
    assert booking.items.filter(variant=variant).exists()
    assert "Added a table" in booking.clearing_override_note
    assert booking.clearing_override_by == staff
    assert booking.needs_attention


def test_overrides_add_to_the_note_rather_than_replace_it(clearing, staff):
    booking, _ = clearing
    lifecycle.change_item(booking, ItemVariantFactory(), quantity=1, by=staff, override_note="One")
    lifecycle.cancel_booking(booking, by=staff, override_note="Two")

    booking.refresh_from_db()
    assert "One" in booking.clearing_override_note
    assert "Two" in booking.clearing_override_note


def test_an_open_card_checkout_cannot_be_overridden(staff):
    booking = held_booking(total="200.00")
    start_checkout(booking, Payment.Kind.FULL)

    with pytest.raises(lifecycle.BookingStateError):
        lifecycle.cancel_booking(booking, by=staff, override_note=NOTE)
    with pytest.raises(lifecycle.BookingStateError):
        lifecycle.change_item(
            booking, ItemVariantFactory(), quantity=1, by=staff, override_note=NOTE
        )


def test_a_pending_refund_cannot_be_overridden(client, staff, refunds):
    booking = held_booking(total="200.00")
    payment = pay(client, booking, Payment.Kind.FULL)
    Payment.objects.create(
        booking=booking,
        amount=D("50.00"),
        method=Payment.Method.CARD,
        kind=Payment.Kind.RENTAL_REFUND,
        refund_of=payment,
    )

    with pytest.raises(lifecycle.BookingStateError):
        lifecycle.cancel_booking(booking, by=staff, override_note=NOTE)


def test_admin_cancel_form_takes_the_override_note(admin_client, admin_user, clearing):
    booking, _ = clearing
    url = reverse("admin:bookings_booking_ledger", args=[booking.pk, "cancel"])

    refused = admin_client.post(url, {})
    assert "override note" in refused.content.decode()

    admin_client.post(url, {"override_note": NOTE})

    booking.refresh_from_db()
    assert booking.status == Status.CANCELLED
    assert booking.clearing_override_by == admin_user
