"""Refund reversals, the catch-up job, first invoices for admin bookings and resending invoices."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking, PaymentChoice
from apps.bookings.tests.factories import ItemVariantFactory
from apps.bookings.tests.helpers import booking_worth
from apps.bookings.tests.test_admin import booking_form
from apps.invoices.models import Invoice
from apps.invoices.services import issuing, sweep
from apps.invoices.tests.helpers import documents, online_booking, pdf_text
from apps.notifications.models import SentEmail
from apps.payments.models import Payment
from apps.payments.services import ledger
from apps.payments.services import stripe as stripe_service
from apps.payments.tests.helpers import charge_refunded_event, event, held_booking, pay, post_event

pytestmark = pytest.mark.django_db
D = Decimal
OWNER = "owner@rentals.example"


@pytest.fixture
def committed(run_tasks, django_capture_on_commit_callbacks):
    def block():
        return django_capture_on_commit_callbacks(execute=True)

    return block


def customer_mail(outbox, booking):
    return [message for message in outbox if booking.email in message.to]


def fail_at_stripe(client, refunds, index=0):
    refunds.refunds[index]["status"] = "failed"
    return post_event(client, event("refund.failed", refunds.refunds[index]))


@pytest.fixture
def card_paid(client, committed):
    booking = held_booking("200.00", caution_fee="50.00")
    with committed():
        payment = pay(client, booking, Payment.Kind.FULL)
    return booking, payment


# Refund reversed


def test_a_refund_that_fails_after_its_receipt_gets_a_correction_and_an_email(
    client, committed, refunds, card_paid, mailoutbox
):
    booking, payment = card_paid
    with committed():
        stripe_service.refund_rental(payment, D("60.00"))
    [receipt] = documents(booking, Invoice.Kind.REFUND_RECEIPT)
    mailoutbox.clear()

    with committed():
        fail_at_stripe(client, refunds)

    [reversal] = documents(booking, Invoice.Kind.REFUND_REVERSAL)
    assert reversal.reverses == receipt
    assert reversal.snapshot["reverses"] == receipt.number
    assert reversal.snapshot["refund"]["amount"] == "60.00"
    assert reversal.snapshot["amount_paid"] == "200.00"
    text = pdf_text(reversal)
    assert "Refund reversed" in text
    assert f"Corrects refund receipt {receipt.number}" in text
    assert "$60.00" in text
    receipt.refresh_from_db()
    assert receipt.snapshot["amount_paid"] == "140.00"

    [message] = customer_mail(mailoutbox, booking)
    assert message.subject == f"Your refund did not go through: Booking #{booking.pk}"
    assert receipt.number in message.body
    assert [name for name, _, _ in message.attachments] == [f"{reversal.number}.pdf"]


def test_a_repeated_failure_report_issues_one_correction(
    client, committed, refunds, card_paid, mailoutbox
):
    booking, payment = card_paid
    with committed():
        stripe_service.refund_rental(payment, D("60.00"))
    mailoutbox.clear()

    with committed():
        fail_at_stripe(client, refunds)
        fail_at_stripe(client, refunds)
    with committed():
        post_event(client, charge_refunded_event(payment, refunds))
        stripe_service.sync_refunds(payment)

    assert len(documents(booking, Invoice.Kind.REFUND_REVERSAL)) == 1
    assert len(customer_mail(mailoutbox, booking)) == 1


def test_a_failed_caution_refund_gets_a_correction(client, committed, refunds, card_paid):
    booking, _ = card_paid
    with committed():
        stripe_service.refund_caution(booking, D("50.00"))

    with committed():
        fail_at_stripe(client, refunds)

    [reversal] = documents(booking, Invoice.Kind.REFUND_REVERSAL)
    assert reversal.snapshot["refund"]["kind"] == "Caution refund"
    assert reversal.snapshot["caution_held"] == "50.00"


def test_a_failed_dashboard_refund_gets_a_correction(client, committed, refunds, card_paid):
    booking, payment = card_paid
    refunds.add(payment, "30.00")
    with committed():
        post_event(client, charge_refunded_event(payment, refunds))
    [receipt] = documents(booking, Invoice.Kind.REFUND_RECEIPT)

    with committed():
        fail_at_stripe(client, refunds)

    [reversal] = documents(booking, Invoice.Kind.REFUND_REVERSAL)
    assert reversal.reverses == receipt
    assert reversal.source_key.startswith("stripe-refund-reversed:")


def test_a_refund_that_fails_before_succeeding_gets_no_correction(
    committed, refunds, card_paid, mailoutbox
):
    booking, payment = card_paid
    entry = ledger.reserve_rental_refund(payment.pk, D("40.00"))
    mailoutbox.clear()

    with committed():
        ledger.fail_refund(entry.pk, Payment.FailureReason.STRIPE_REFUSED)

    assert documents(booking, Invoice.Kind.REFUND_RECEIPT) == []
    assert documents(booking, Invoice.Kind.REFUND_REVERSAL) == []
    assert customer_mail(mailoutbox, booking) == []


def test_no_correction_without_a_refund_receipt_to_correct(card_paid):
    booking, payment = card_paid
    entry = Payment.objects.create(
        booking=booking,
        amount=D("10.00"),
        kind=Payment.Kind.RENTAL_REFUND,
        status=Payment.Status.FAILED,
        refund_of=payment,
    )

    assert issuing.issue_refund_reversal(entry.pk) == (None, False)


# The catch-up job


def later(minutes=10):
    return timezone.now() + timedelta(minutes=minutes)


def test_the_catch_up_job_issues_lost_documents_and_queues_their_emails(
    committed, run_tasks, event_date, mailoutbox
):
    # Every on-commit job below is lost: nothing here runs its callbacks.
    booking = online_booking(event_date, caution="10.00", choice=PaymentChoice.ZELLE)
    zelle = dict(received_at=timezone.now(), received_by=None, method=Payment.Method.ZELLE)
    ledger.record_manual_payment(
        booking, amount=D("80.00"), caution_amount=D("40.00"), reference="A", **zelle
    )
    ledger.record_caution_refund(booking, amount=D("15.00"), reference="R", **zelle)
    assert documents(booking) == []
    assert mailoutbox == []

    with committed():
        counts = sweep.issue_missing(later())

    assert counts == {"invoices": 1, "receipts": 1, "refund_receipts": 1}
    kinds = [doc.kind for doc in documents(booking)]
    assert sorted(kinds) == sorted(
        [Invoice.Kind.INVOICE, Invoice.Kind.RECEIPT, Invoice.Kind.REFUND_RECEIPT]
    )
    # No "booking received": by the time the job runs the booking is already paid.
    subjects = sorted(message.subject for message in mailoutbox)
    assert subjects == sorted(
        [
            f"New booking #{booking.pk}: Ada Planner, {booking.event_date:%b %d, %Y}",
            f"Your booking is confirmed: Booking #{booking.pk}",
            f"Payment of $120.00 on booking #{booking.pk} (Ada Planner)",
            f"Your refund: Booking #{booking.pk}",
        ]
    )


def test_running_the_catch_up_job_again_does_nothing_more(committed, event_date, mailoutbox):
    booking = online_booking(event_date, choice=PaymentChoice.ZELLE)
    ledger.record_manual_payment(
        booking,
        amount=D("80.00"),
        received_at=timezone.now(),
        reference="A",
        received_by=None,
        method=Payment.Method.ZELLE,
    )
    with committed():
        sweep.issue_missing(later())
    issued, sent = len(documents(booking)), len(mailoutbox)

    with committed():
        counts = sweep.issue_missing(later())
    from apps.invoices import tasks

    tasks.issue_booking_invoice.apply(args=[booking.pk], throw=True)

    assert counts == {"invoices": 0, "receipts": 0, "refund_receipts": 0}
    assert len(documents(booking)) == issued
    assert len(mailoutbox) == sent


def test_the_catch_up_job_leaves_documents_issued_normally_alone(committed, event_date, mailoutbox):
    with committed():
        booking = online_booking(event_date, choice=PaymentChoice.ZELLE)
    with committed():
        ledger.record_manual_payment(
            booking,
            amount=D("80.00"),
            received_at=timezone.now(),
            reference="A",
            received_by=None,
            method=Payment.Method.ZELLE,
        )
    sent = len(mailoutbox)

    with committed():
        counts = sweep.issue_missing(later())

    assert counts == {"invoices": 0, "receipts": 0, "refund_receipts": 0}
    assert len(mailoutbox) == sent


def test_the_catch_up_job_skips_rows_too_new_or_too_old(committed, event_date, mailoutbox):
    online_booking(event_date, choice=PaymentChoice.ZELLE)

    with committed():
        too_new = sweep.issue_missing(timezone.now())
        too_old = sweep.issue_missing(timezone.now() + timedelta(days=4))

    assert too_new == too_old == {"invoices": 0, "receipts": 0, "refund_receipts": 0}
    assert not Invoice.objects.exists()
    assert mailoutbox == []


def test_the_catch_up_job_invoices_admin_bookings_without_emailing(committed, mailoutbox):
    booking = booking_worth("100.00", Booking.Status.QUOTED)

    with committed():
        sweep.issue_missing(later())

    [invoice] = documents(booking)
    assert invoice.kind == Invoice.Kind.INVOICE
    assert mailoutbox == []


def test_the_catch_up_job_runs_every_fifteen_minutes():
    schedule = settings.CELERY_BEAT_SCHEDULE["issue-missing-documents"]
    assert schedule == {"task": "invoices.issue_missing_documents", "schedule": 900}


# Admin bookings: first invoice when lines are first saved


def test_an_admin_booking_is_invoiced_when_saved_with_lines_and_not_emailed(
    admin_client, committed, event_date, mailoutbox
):
    variant = ItemVariantFactory(stock_quantity=10, item__base_price=D("25.00"))

    with committed():
        response = admin_client.post(
            reverse("admin:bookings_booking_add"),
            booking_form(
                Booking.Status.QUOTED,
                [{"variant": variant, "quantity": 2, "unit_price": "25.00"}],
                event_date=event_date,
            ),
        )

    assert response.status_code == 302
    booking = Booking.objects.get()
    [invoice] = documents(booking)
    assert invoice.kind == Invoice.Kind.INVOICE
    assert invoice.snapshot["total"] == "50.00"
    assert mailoutbox == []
    assert not SentEmail.objects.exists()


def test_an_admin_booking_without_lines_is_invoiced_once_lines_are_added(
    admin_client, committed, event_date
):
    with committed():
        admin_client.post(
            reverse("admin:bookings_booking_add"),
            booking_form(Booking.Status.ENQUIRY, [], event_date=event_date),
        )
    booking = Booking.objects.get()
    assert documents(booking) == []

    variant = ItemVariantFactory(stock_quantity=10)
    with committed():
        admin_client.post(
            reverse("admin:bookings_booking_change", args=[booking.pk]),
            booking_form(
                Booking.Status.ENQUIRY,
                [{"variant": variant, "quantity": 3, "unit_price": "10.00"}],
                event_date=event_date,
            ),
        )

    [invoice] = documents(booking)
    assert invoice.snapshot["total"] == "30.00"
    assert invoice.source_key == f"booking:{booking.pk}"


# Send invoice to customer


def send_action(admin_client, *bookings):
    return admin_client.post(
        reverse("admin:bookings_booking_changelist"),
        {"action": "send_invoice_to_customer", "_selected_action": [b.pk for b in bookings]},
        follow=True,
    )


def test_staff_send_the_latest_invoice_with_its_pdf(admin_client, committed, mailoutbox):
    booking = booking_worth("100.00", Booking.Status.QUOTED)
    issuing.issue_manual_invoice(booking, note="First")
    latest = issuing.issue_manual_invoice(booking, note="Second")

    with committed():
        response = send_action(admin_client, booking)

    assert f"Invoice emailed for booking #{booking.pk}" in response.content.decode()
    [message] = mailoutbox
    assert message.to == [booking.email]
    assert message.subject == f"Your invoice {latest.number}: Booking #{booking.pk}"
    assert [name for name, _, _ in message.attachments] == [f"{latest.number}.pdf"]
    assert message.attachments[0][1].startswith(b"%PDF")
    assert "https://rentals.example/booking/" in message.body
    assert "https://rentals.example/payments/balance/" not in message.body  # quoted, not held


def test_sending_again_sends_again(admin_client, committed, mailoutbox):
    booking = booking_worth("100.00", Booking.Status.QUOTED)
    issuing.issue_manual_invoice(booking)

    with committed():
        send_action(admin_client, booking)
    with committed():
        send_action(admin_client, booking)

    assert len(mailoutbox) == 2


def test_a_booking_without_an_invoice_is_reported_and_nothing_is_sent(
    admin_client, committed, mailoutbox
):
    booking = Booking.objects.create(
        customer_name="No Lines",
        email="x@example.com",
        phone="1",
        event_date=timezone.localdate(),
        delivery_address="Somewhere",
        status=Booking.Status.ENQUIRY,
    )

    with committed():
        response = send_action(admin_client, booking)

    assert f"No invoice yet for booking #{booking.pk}" in response.content.decode()
    assert mailoutbox == []


def test_the_catch_up_job_sends_booking_received_for_a_lost_manual_booking_on_hold(
    committed, event_date, mailoutbox
):
    booking = online_booking(event_date, choice=PaymentChoice.CASHAPP)

    with committed():
        sweep.issue_missing(later())

    assert [doc.kind for doc in documents(booking)] == [Invoice.Kind.INVOICE]
    customer = [m.subject for m in mailoutbox if booking.email in m.to]
    assert customer == [f"We received your booking: Booking #{booking.pk}"]
    assert any(OWNER in m.to for m in mailoutbox)
