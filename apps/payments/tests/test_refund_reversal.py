"""Refunds that fail after being counted, promo uses given back, and refund paging."""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.services import lifecycle
from apps.bookings.services.quote import LineRequest, create_booking_from_quote
from apps.bookings.tests.factories import (
    EVENT_DATE,
    DeliveryZoneFactory,
    ItemVariantFactory,
    PromoCodeFactory,
)
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
Status = Booking.Status
RENTAL = stripe_service.RENTAL_REFUND


@pytest.fixture
def paid(client):
    """A $200 booking paid in full by card."""
    booking = held_booking(total="200.00")
    return booking, pay(client, booking, Payment.Kind.FULL)


def fail_at_stripe(client, refunds, index=0):
    """Stripe reports a refund it had accepted as failed: list and webhook both say so."""
    refunds.refunds[index]["status"] = "failed"
    return post_event(client, event("refund.failed", refunds.refunds[index]))


def test_failed_partial_refund_puts_the_money_back_and_restores_the_status(client, paid, refunds):
    booking, payment = paid
    stripe_service.refund_rental(payment, D("90.00"))
    booking.refresh_from_db()
    assert booking.status == Status.DEPOSIT_PAID

    assert fail_at_stripe(client, refunds).status_code == 200

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert payment.refunded_amount == D("0.00")
    assert payment.status == Payment.Status.SUCCEEDED
    assert booking.amount_paid == D("200.00")
    assert booking.status == Status.PAID
    assert booking.needs_attention
    entry = booking.payments.get(kind=Payment.Kind.RENTAL_REFUND)
    assert entry.status == Payment.Status.FAILED


def test_failed_full_refund_restores_the_money_and_leaves_the_booking_for_staff(
    client, paid, refunds
):
    booking, payment = paid
    stripe_service.refund_rental(payment, D("200.00"))
    payment.refresh_from_db()
    assert payment.status == Payment.Status.REFUNDED

    fail_at_stripe(client, refunds)

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert payment.refunded_amount == D("0.00")
    assert payment.status == Payment.Status.SUCCEEDED
    assert booking.amount_paid == D("200.00")
    assert booking.status == Status.CANCELLED
    assert booking.needs_attention


def test_failure_reported_only_by_the_webhook_still_counts(client, paid, refunds):
    """Stripe's list can lag: the refund.failed event itself is enough."""
    booking, payment = paid
    stripe_service.refund_rental(payment, D("60.00"))
    failed = {**refunds.refunds[0], "status": "failed"}

    post_event(client, event("refund.failed", failed))

    payment.refresh_from_db()
    assert payment.refunded_amount == D("0.00")


def test_repeated_failure_reports_change_nothing_more(client, paid, refunds):
    booking, payment = paid
    stripe_service.refund_rental(payment, D("60.00"))
    stripe_service.refund_rental(payment, D("40.00"))
    fail_at_stripe(client, refunds, index=0)

    fail_at_stripe(client, refunds, index=0)
    post_event(client, charge_refunded_event(payment, refunds))
    stripe_service.sync_refunds(payment)

    payment.refresh_from_db()
    assert payment.refunded_amount == D("40.00")
    entries = booking.payments.filter(kind=Payment.Kind.RENTAL_REFUND).order_by("pk")
    assert [entry.status for entry in entries] == [Payment.Status.FAILED, Payment.Status.SUCCEEDED]


def test_lagging_list_does_not_undo_a_refund_that_went_through(client, paid, refunds):
    """Only refunds Stripe says failed are taken back, not ones missing from the list."""
    booking, payment = paid
    stripe_service.refund_rental(payment, D("60.00"))
    stripe_service.refund_rental(payment, D("40.00"))
    refunds.refunds[0]["status"] = "failed"
    del refunds.refunds[1]

    stripe_service.sync_refunds(payment)

    payment.refresh_from_db()
    assert payment.refunded_amount == D("40.00")


def test_failed_dashboard_refund_is_taken_back(client, paid, refunds):
    booking, payment = paid
    refunds.add(payment, "50.00")
    post_event(client, charge_refunded_event(payment, refunds))
    payment.refresh_from_db()
    assert payment.refunded_amount == D("50.00")

    fail_at_stripe(client, refunds)

    payment.refresh_from_db()
    assert payment.refunded_amount == D("0.00")


def test_sync_reads_every_page_of_refunds(paid, refunds):
    booking, payment = paid
    for _ in range(120):
        refunds.add(payment, "1.00", kind=RENTAL)

    stripe_service.sync_refunds(payment)

    payment.refresh_from_db()
    assert payment.refunded_amount == D("120.00")
    assert Booking.objects.get(pk=booking.pk).amount_paid == D("80.00")


def test_full_rental_refund_with_caution_still_held_flags_the_booking(client, refunds):
    booking = held_booking(total="200.00", caution_fee="75.00")
    payment = pay(client, booking, Payment.Kind.FULL)

    stripe_service.refund_rental(payment, D("200.00"))

    booking.refresh_from_db()
    assert booking.status == Status.CANCELLED
    assert booking.caution_held == D("75.00")
    assert booking.needs_attention


def test_full_rental_refund_without_caution_is_not_flagged(paid, refunds):
    booking, payment = paid
    stripe_service.refund_rental(payment, D("200.00"))
    booking.refresh_from_db()
    assert booking.status == Status.CANCELLED
    assert not booking.needs_attention


@pytest.fixture
def promo(settings):
    settings.SALES_TAX_RATE = D("0")
    DeliveryZoneFactory(zip_codes=["75001"], delivery_fee=D("0.00"), setup_fee=D("0.00"))
    return PromoCodeFactory(code="ONCE", percent_off=D("10"), max_uses=1)


def booking_with_promo():
    """A held booking for $200 of items, $180 after the 10% promo."""
    variant = ItemVariantFactory(item__base_price=D("200.00"))
    return create_booking_from_quote(
        [LineRequest(variant_id=variant.pk, quantity=1)],
        EVENT_DATE,
        "75001",
        "ONCE",
        customer_name="Ada Planner",
        email="ada@example.com",
        phone="555-0100",
        delivery_address="1 Main St, Dallas, TX",
    )


def uses(promo):
    promo.refresh_from_db()
    return promo.uses_count


def test_full_refund_gives_the_promo_use_back(client, promo, refunds):
    booking = booking_with_promo()
    payment = pay(client, booking, Payment.Kind.FULL)
    assert uses(promo) == 1

    stripe_service.refund_rental(payment, D("180.00"))

    booking.refresh_from_db()
    assert booking.status == Status.CANCELLED
    assert uses(promo) == 0
    assert booking.promo_redeemed_at is None


def test_partial_refund_keeps_the_promo_use(client, promo, refunds):
    booking = booking_with_promo()
    payment = pay(client, booking, Payment.Kind.FULL)

    stripe_service.refund_rental(payment, D("50.00"))

    assert uses(promo) == 1


def test_cancelling_gives_the_promo_use_back_once(client, promo, refunds):
    booking = booking_with_promo()
    payment = pay(client, booking, Payment.Kind.FULL)

    lifecycle.cancel_booking(booking)
    assert uses(promo) == 0
    stripe_service.refund_rental(payment, D("180.00"))

    assert uses(promo) == 0
    booking.refresh_from_db()
    assert booking.promo_redeemed_at is None


def test_cancelling_an_unpaid_booking_leaves_the_promo_alone(promo):
    promo.uses_count = 1
    promo.max_uses = 5
    promo.save()
    booking = booking_with_promo()

    lifecycle.cancel_booking(booking)

    assert uses(promo) == 1


def test_manual_full_refund_gives_the_promo_use_back(promo):
    booking = booking_with_promo()
    staff = get_user_model().objects.create_user("owner", is_staff=True)
    payment = ledger.record_manual_payment(
        booking,
        method=Payment.Method.ZELLE,
        amount=D("180.00"),
        received_at=timezone.now(),
        reference="Z-1",
        received_by=staff,
    )
    assert uses(promo) == 1

    ledger.record_manual_rental_refund(
        payment,
        amount=D("180.00"),
        method=Payment.Method.ZELLE,
        reference="Z-R1",
        received_at=timezone.now(),
        received_by=staff,
    )

    assert uses(promo) == 0
