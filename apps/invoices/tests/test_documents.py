"""The right document for each event, issued once, after commit, from the existing services."""

from datetime import timedelta
from decimal import Decimal

import pytest
import stripe
from django.db import transaction
from django.utils import timezone

from apps.bookings.models import Booking, PaymentChoice
from apps.bookings.services import lifecycle
from apps.bookings.tests.factories import ItemVariantFactory
from apps.invoices.models import Invoice
from apps.invoices.tests.helpers import documents, online_booking
from apps.payments.models import Payment
from apps.payments.services import ledger
from apps.payments.services import stripe as stripe_service
from apps.payments.tests.helpers import (
    charge_refunded_event,
    event,
    held_booking,
    pay,
    post_event,
    session_object,
    start_checkout,
    stripe_sessions,
)

pytestmark = pytest.mark.django_db
D = Decimal


@pytest.fixture
def committed(run_tasks, django_capture_on_commit_callbacks):
    """Run what the block commits: its on-commit jobs, and the jobs those queue."""

    def block():
        return django_capture_on_commit_callbacks(execute=True)

    return block


def manual_payment(booking, amount, caution="0.00", reference="ZL-1"):
    return ledger.record_manual_payment(
        booking,
        method=Payment.Method.ZELLE,
        amount=D(amount),
        caution_amount=D(caution),
        received_at=timezone.now(),
        reference=reference,
        received_by=None,
    )


def test_an_online_booking_is_invoiced_once_committed(committed, event_date):
    with committed():
        booking = online_booking(event_date)

    [invoice] = documents(booking)
    assert invoice.kind == Invoice.Kind.INVOICE
    assert invoice.payment is None
    snap = invoice.snapshot
    assert snap["items"] == [
        {
            "name": "Gold Chiavari Chair (Default)",
            "quantity": 4,
            "unit_price": "25.00",
            "amount": "100.00",
        }
    ]
    assert (snap["subtotal"], snap["delivery_fee"], snap["setup_fee"]) == (
        "100.00",
        "40.00",
        "20.00",
    )
    assert (snap["total"], snap["amount_paid"], snap["balance_due"]) == ("160.00", "0.00", "160.00")
    assert snap["billed_to"]["name"] == "Ada Planner"
    assert snap["event"]["date"] == event_date.isoformat()


def test_nothing_is_issued_until_commit_and_never_after_a_rollback(
    queued_tasks, run_tasks, event_date
):
    with pytest.raises(RuntimeError), transaction.atomic():
        online_booking(event_date)
        raise RuntimeError("the booking flow failed after creating the booking")

    assert not Invoice.objects.exists()
    assert run_tasks == []


def test_the_invoice_job_is_queued_on_commit_only(queued_tasks, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        online_booking(timezone.localdate() + timedelta(days=60))
    assert queued_tasks == []

    for callback in callbacks:
        callback()
    assert [name for name, _ in queued_tasks] == ["invoices.issue_booking_invoice"]


def test_a_card_payment_gets_a_receipt(client, committed):
    booking = held_booking("200.00")

    with committed():
        payment = pay(client, booking, Payment.Kind.DEPOSIT)

    [receipt] = documents(booking, Invoice.Kind.RECEIPT)
    assert receipt.payment == payment
    snap = receipt.snapshot
    assert snap["payment"]["total"] == "100.00"
    assert snap["payment"]["method"] == "Card"
    assert (snap["amount_paid"], snap["balance_due"]) == ("100.00", "100.00")


def test_a_repeated_webhook_issues_nothing_twice(client, committed, mailoutbox):
    booking = held_booking("200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    stripe_sessions().complete(payment.stripe_checkout_session_id)
    completed = event("checkout.session.completed", session_object(payment))

    with committed():
        post_event(client, completed)
    with committed():
        post_event(client, completed)
        # The same payment reported again under a new event id.
        post_event(client, event("checkout.session.completed", session_object(payment)))
        ledger.record_success(payment.pk)

    assert len(documents(booking, Invoice.Kind.RECEIPT)) == 1
    assert len([m for m in mailoutbox if booking.email in m.to]) == 1


def test_a_retried_job_issues_nothing_twice(event_date, committed):
    with committed():
        booking = online_booking(event_date)
        payment = manual_payment(booking, "80.00")

    from apps.invoices import tasks

    tasks.issue_booking_invoice.apply(args=[booking.pk], throw=True)
    tasks.issue_payment_documents.apply(args=[payment.pk, True], throw=True)

    assert [doc.kind for doc in documents(booking)] == [Invoice.Kind.INVOICE, Invoice.Kind.RECEIPT]


def test_a_failed_payment_gets_no_receipt(client, committed):
    booking = held_booking("200.00")
    payment, _ = start_checkout(booking, Payment.Kind.DEPOSIT)

    with committed():
        post_event(client, event("checkout.session.expired", session_object(payment, "unpaid")))

    assert documents(booking) == []


def test_a_manual_payment_gets_a_receipt(committed, event_date):
    with committed():
        booking = online_booking(event_date, choice=PaymentChoice.ZELLE)
    with committed():
        payment = manual_payment(booking, "80.00", reference="ZELLE-778")

    [receipt] = documents(booking, Invoice.Kind.RECEIPT)
    assert receipt.payment == payment
    assert receipt.snapshot["payment"]["reference"] == "ZELLE-778"
    assert receipt.snapshot["payment"]["method"] == "Zelle"
    assert receipt.snapshot["balance_due"] == "80.00"


def test_each_payment_gets_its_own_receipt(committed, event_date):
    with committed():
        booking = online_booking(event_date, choice=PaymentChoice.ZELLE)
        manual_payment(booking, "80.00", reference="A")
    with committed():
        manual_payment(booking, "80.00", reference="B")

    receipts = documents(booking, Invoice.Kind.RECEIPT)
    assert [r.snapshot["balance_due"] for r in receipts] == ["80.00", "0.00"]
    assert receipts[0].number < receipts[1].number


def test_an_ach_payment_is_receipted_when_it_clears_not_while_processing(client, committed):
    booking = held_booking("200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    stripe_sessions().complete(payment.stripe_checkout_session_id, payment_status="unpaid")

    with committed():
        post_event(client, event("checkout.session.completed", session_object(payment, "unpaid")))
    assert documents(booking) == []

    with committed():
        post_event(
            client,
            event("checkout.session.async_payment_succeeded", session_object(payment, "paid")),
        )
    [receipt] = documents(booking, Invoice.Kind.RECEIPT)
    assert receipt.snapshot["payment"]["method"] == "ACH bank transfer"


def test_a_rental_refund_through_stripe_gets_a_refund_receipt(client, committed, refunds):
    booking = held_booking("200.00")
    with committed():
        payment = pay(client, booking, Payment.Kind.FULL)

    with committed():
        stripe_service.refund_rental(payment, D("50.00"))

    [refund_receipt] = documents(booking, Invoice.Kind.REFUND_RECEIPT)
    assert refund_receipt.payment == Payment.objects.get(kind=Payment.Kind.RENTAL_REFUND)
    assert refund_receipt.payment.refund_of == payment
    refund = refund_receipt.snapshot["refund"]
    assert (refund["kind"], refund["amount"]) == ("Rental refund", "50.00")
    assert refund_receipt.snapshot["amount_paid"] == "150.00"

    # Stripe's webhook for the same refund issues nothing more.
    with committed():
        post_event(client, charge_refunded_event(payment, refunds))
    assert len(documents(booking, Invoice.Kind.REFUND_RECEIPT)) == 1


def test_a_refund_made_in_the_stripe_dashboard_gets_one_refund_receipt(client, committed, refunds):
    booking = held_booking("200.00")
    with committed():
        payment = pay(client, booking, Payment.Kind.FULL)
    refunds.add(payment, "30.00")
    refunded = charge_refunded_event(payment, refunds)

    with committed():
        post_event(client, refunded)
        stripe_service.sync_booking(booking)

    [receipt] = documents(booking, Invoice.Kind.REFUND_RECEIPT)
    assert receipt.payment == payment
    assert receipt.snapshot["refund"]["amount"] == "30.00"
    assert receipt.source_key.startswith("stripe-refund:")


def test_a_caution_refund_through_stripe_gets_a_refund_receipt(client, committed, refunds):
    booking = held_booking("200.00", caution_fee="50.00")
    with committed():
        pay(client, booking, Payment.Kind.FULL)

    with committed():
        stripe_service.refund_caution(booking, D("50.00"))

    [receipt] = documents(booking, Invoice.Kind.REFUND_RECEIPT)
    assert receipt.snapshot["refund"]["kind"] == "Caution refund"
    assert receipt.snapshot["refund"]["amount"] == "50.00"
    assert receipt.snapshot["caution_held"] == "0.00"


def test_a_manual_caution_refund_gets_a_refund_receipt_and_a_damage_charge_does_not(
    committed, event_date
):
    with committed():
        booking = online_booking(event_date, caution="10.00", choice=PaymentChoice.ZELLE)
        manual_payment(booking, "160.00", caution="40.00")

    with committed():
        ledger.record_damage_charge(
            booking,
            amount=D("15.00"),
            reference="Torn cover",
            received_at=timezone.now(),
            received_by=None,
        )
        ledger.record_caution_refund(
            booking,
            amount=D("25.00"),
            method=Payment.Method.ZELLE,
            reference="ZR-1",
            received_at=timezone.now(),
            received_by=None,
        )

    [receipt] = documents(booking, Invoice.Kind.REFUND_RECEIPT)
    assert receipt.snapshot["refund"]["amount"] == "25.00"
    assert receipt.snapshot["refund"]["reference"] == "ZR-1"


def test_a_manual_rental_refund_gets_a_refund_receipt(committed, event_date):
    with committed():
        booking = online_booking(event_date, choice=PaymentChoice.ZELLE)
        payment = manual_payment(booking, "160.00")

    with committed():
        ledger.record_manual_rental_refund(
            payment,
            amount=D("20.00"),
            method=Payment.Method.ZELLE,
            reference="RF-9",
            received_at=timezone.now(),
            received_by=None,
        )

    [receipt] = documents(booking, Invoice.Kind.REFUND_RECEIPT)
    assert receipt.snapshot["refund"]["amount"] == "20.00"
    assert receipt.snapshot["amount_paid"] == "140.00"


def test_a_failed_stripe_refund_gets_no_refund_receipt(client, committed, refunds):
    booking = held_booking("200.00")
    with committed():
        payment = pay(client, booking, Payment.Kind.FULL)

    def refuse(kwargs):
        raise stripe.InvalidRequestError("No such charge", None)

    refunds.on_create = refuse

    with committed(), pytest.raises((ledger.PaymentError, stripe.StripeError)):
        stripe_service.refund_rental(payment, D("50.00"))

    assert Payment.objects.get(kind=Payment.Kind.RENTAL_REFUND).status == Payment.Status.FAILED
    assert documents(booking, Invoice.Kind.REFUND_RECEIPT) == []


def test_a_change_to_the_booking_issues_a_revised_invoice_and_keeps_the_original(
    committed, event_date
):
    with committed():
        booking = online_booking(event_date, choice=PaymentChoice.ZELLE)
        manual_payment(booking, "80.00")
    [original] = documents(booking, Invoice.Kind.INVOICE)
    before = dict(original.snapshot)

    with committed():
        lifecycle.change_item(
            booking, ItemVariantFactory(stock_quantity=5), quantity=2, unit_price=D("15.00")
        )

    original.refresh_from_db()
    assert original.snapshot == before
    assert original.snapshot["total"] == "160.00"
    revised = documents(booking, Invoice.Kind.INVOICE)[-1]
    assert revised.supersedes == original
    assert revised.snapshot["supersedes"] == original.number
    assert revised.snapshot["total"] == "190.00"
    assert len(revised.snapshot["items"]) == 2


def test_an_edit_that_changes_no_money_issues_no_revised_invoice(committed, event_date):
    with committed():
        booking = online_booking(event_date, choice=PaymentChoice.ZELLE)
        manual_payment(booking, "80.00")

    with committed():
        lifecycle.set_tax_exemption(booking, exempt=False, certificate="", by=None)

    assert len(documents(booking, Invoice.Kind.INVOICE)) == 1


def test_a_tax_exemption_issues_a_revised_invoice(committed, event_date, settings, admin_user):
    settings.SALES_TAX_RATE = D("8.25")
    with committed():
        booking = online_booking(event_date, choice=PaymentChoice.ZELLE)
    [original] = documents(booking, Invoice.Kind.INVOICE)
    assert original.snapshot["tax"] == "8.25"

    with committed():
        lifecycle.set_tax_exemption(booking, exempt=True, certificate="TX-01-234", by=admin_user)

    revised = documents(booking, Invoice.Kind.INVOICE)[-1]
    assert revised.snapshot["tax"] == "0.00"
    assert revised.snapshot["tax_exempt"] is True
    assert revised.snapshot["tax_exempt_certificate"] == "TX-01-234"
    assert original.snapshot["tax"] == "8.25"


def test_editing_a_booking_directly_leaves_issued_snapshots_alone(committed, event_date):
    with committed():
        booking = online_booking(event_date)
    [invoice] = documents(booking)
    Booking.objects.filter(pk=booking.pk).update(delivery_fee=D("99.00"), discount=D("10.00"))
    booking.items.update(unit_price=D("1.00"))

    invoice.refresh_from_db()
    assert invoice.snapshot["delivery_fee"] == "40.00"
    assert invoice.snapshot["total"] == "160.00"
