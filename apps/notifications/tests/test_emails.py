"""Customer and owner emails: content, links, attachments, sent once and only after commit."""

from decimal import Decimal
from unittest import mock

import pytest
from django.core import mail
from django.db import transaction
from django.utils import timezone

from apps.bookings.models import Booking, PaymentChoice, PaymentPlan
from apps.invoices.models import Invoice
from apps.invoices.tests.helpers import documents, online_booking
from apps.notifications.models import SentEmail
from apps.notifications.services import emails
from apps.payments.models import Payment
from apps.payments.services import ledger
from apps.payments.services import stripe as stripe_service
from apps.payments.tests.helpers import (
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
OWNER = "owner@rentals.example"


@pytest.fixture
def committed(run_tasks, django_capture_on_commit_callbacks):
    def block():
        return django_capture_on_commit_callbacks(execute=True)

    return block


def to_customer(outbox, booking=None):
    email = booking.email if booking else "ada@example.com"
    return [message for message in outbox if email in message.to]


def to_owner(outbox):
    return [message for message in outbox if OWNER in message.to]


def html(message):
    return message.alternatives[0][0]


def zelle_payment(booking, amount, caution="0.00", reference="ZL-1"):
    return ledger.record_manual_payment(
        booking,
        method=Payment.Method.ZELLE,
        amount=D(amount),
        caution_amount=D(caution),
        received_at=timezone.now(),
        reference=reference,
        received_by=None,
    )


def assert_common(message, booking, *, balance_link):
    """Reference, status link and (when a balance is due) balance link, in both bodies."""
    for body in (message.body, html(message)):
        assert f"Booking #{booking.pk}" in body
        assert "https://rentals.example/booking/" in body
        assert ("https://rentals.example/payments/balance/" in body) is balance_link
    assert message.from_email == "bookings@rentals.example"
    assert message.reply_to == [OWNER]


def test_manual_booking_received_has_amount_deadline_and_instructions(
    committed, event_date, mailoutbox
):
    with committed():
        booking = online_booking(event_date, choice=PaymentChoice.ZELLE, plan=PaymentPlan.DEPOSIT)

    [message] = to_customer(mailoutbox)
    assert message.subject == f"We received your booking: Booking #{booking.pk}"
    deadline = timezone.localtime(booking.hold_expires_at)
    for body in (message.body, html(message)):
        assert "$80.00" in body  # 50% deposit of $160
        assert f"{deadline:%B}"[:3] in body
        assert deadline.strftime("%Y") in body
        assert "pay@rentals.example" in body
        assert "Atglace Events" in body
        assert f"Booking {booking.pk}" in body  # the payment memo
        assert "hold ends" in body
    assert_common(message, booking, balance_link=True)
    assert message.attachments == []


def test_manual_booking_received_shows_caution_in_the_amount_to_send(
    committed, event_date, mailoutbox
):
    with committed():
        online_booking(
            event_date, caution="5.00", choice=PaymentChoice.CASHAPP, plan=PaymentPlan.FULL
        )

    [message] = to_customer(mailoutbox)
    assert "$180.00" in message.body  # $160 rental + $20 caution
    assert "$20.00 refundable caution deposit" in message.body
    assert "$AtglaceEvents" in message.body
    assert "pay@rentals.example" not in message.body


def test_bank_deposit_booking_received_lists_the_bank_details(committed, event_date, mailoutbox):
    with committed():
        online_booking(event_date, choice=PaymentChoice.BANK_DEPOSIT)

    [message] = to_customer(mailoutbox)
    for detail in ("First Bank", "Atglace Events LLC", "123456789", "111000025"):
        assert detail in message.body
        assert detail in html(message)


def test_card_booking_received_has_no_manual_instructions(committed, event_date, mailoutbox):
    with committed():
        online_booking(event_date, choice=PaymentChoice.STRIPE)

    [message] = to_customer(mailoutbox)
    assert "complete your payment" in message.body
    assert "pay@rentals.example" not in message.body


def test_no_booking_received_email_once_the_booking_is_already_paid(
    committed, event_date, mailoutbox
):
    with committed():
        booking = online_booking(event_date)
        Booking.objects.filter(pk=booking.pk).update(
            status=Booking.Status.PAID, hold_expires_at=None
        )

    assert [m.subject for m in to_customer(mailoutbox)] == []


def test_owner_is_told_about_each_new_booking(committed, event_date, mailoutbox):
    with committed():
        booking = online_booking(event_date, choice=PaymentChoice.ZELLE)

    [message] = to_owner(mailoutbox)
    assert message.subject.startswith(f"New booking #{booking.pk}: Ada Planner")
    assert "$160.00" in message.body
    assert "Zelle" in message.body
    assert f"/bookings/booking/{booking.pk}/change/" in message.body
    assert "payments/balance" not in message.body


def test_the_payment_that_secures_a_booking_sends_one_confirmation_with_invoice_and_receipt(
    committed, event_date, mailoutbox
):
    with committed():
        booking = online_booking(event_date, choice=PaymentChoice.ZELLE)
    mailoutbox.clear()

    with committed():
        zelle_payment(booking, "80.00")

    [message] = to_customer(mailoutbox)
    assert message.subject == f"Your booking is confirmed: Booking #{booking.pk}"
    invoice = documents(booking, Invoice.Kind.INVOICE)[0]
    receipt = documents(booking, Invoice.Kind.RECEIPT)[0]
    assert [(name, mimetype) for name, _, mimetype in message.attachments] == [
        (f"{invoice.number}.pdf", "application/pdf"),
        (f"{receipt.number}.pdf", "application/pdf"),
    ]
    assert all(content.startswith(b"%PDF") for _, content, _ in message.attachments)
    assert "$80.00" in message.body
    assert_common(message, booking, balance_link=True)


def test_later_payments_send_a_receipt_and_no_balance_link_once_paid(
    committed, event_date, mailoutbox
):
    with committed():
        booking = online_booking(event_date, choice=PaymentChoice.ZELLE)
    with committed():
        zelle_payment(booking, "80.00", reference="A")
    mailoutbox.clear()

    with committed():
        zelle_payment(booking, "80.00", reference="B")

    [message] = to_customer(mailoutbox)
    assert message.subject == f"Payment received: Booking #{booking.pk}"
    receipt = documents(booking, Invoice.Kind.RECEIPT)[-1]
    assert [name for name, _, _ in message.attachments] == [f"{receipt.number}.pdf"]
    assert "Balance due: $0.00" in message.body
    assert_common(message, booking, balance_link=False)


def test_owner_is_told_about_each_payment(committed, event_date, mailoutbox):
    with committed():
        booking = online_booking(event_date, choice=PaymentChoice.ZELLE)
    mailoutbox.clear()

    with committed():
        zelle_payment(booking, "80.00", caution="0.00", reference="ZELLE-42")

    [message] = to_owner(mailoutbox)
    assert message.subject == f"Payment of $80.00 on booking #{booking.pk} (Ada Planner)"
    assert "ZELLE-42" in message.body
    assert "Deposit paid" in message.body
    assert message.attachments == []


def test_a_card_payment_by_webhook_confirms_once_even_when_repeated(client, committed, mailoutbox):
    booking = held_booking("200.00")
    payment, _ = start_checkout(booking, Payment.Kind.DEPOSIT)
    stripe_sessions().complete(payment.stripe_checkout_session_id)
    completed = event("checkout.session.completed", session_object(payment))

    with committed():
        post_event(client, completed)
        post_event(client, completed)
    with committed():
        post_event(client, event("checkout.session.completed", session_object(payment)))

    assert [m.subject for m in to_customer(mailoutbox, booking)] == [
        f"Your booking is confirmed: Booking #{booking.pk}"
    ]
    assert len(to_owner(mailoutbox)) == 1


def test_ach_processing_notice_is_sent_once_and_the_receipt_when_it_clears(
    client, committed, mailoutbox
):
    booking = held_booking("200.00")
    payment, _ = start_checkout(booking, Payment.Kind.FULL)
    stripe_sessions().complete(payment.stripe_checkout_session_id, payment_status="unpaid")
    processing = session_object(payment, "unpaid")

    with committed():
        post_event(client, event("checkout.session.completed", processing))
        ledger.record_processing(payment.pk)

    [notice] = to_customer(mailoutbox, booking)
    assert notice.subject == f"Your bank payment is processing: Booking #{booking.pk}"
    assert "$200.00" in notice.body
    assert "business days" in notice.body
    assert_common(notice, booking, balance_link=True)

    with committed():
        post_event(
            client,
            event("checkout.session.async_payment_succeeded", session_object(payment, "paid")),
        )
    subjects = [m.subject for m in to_customer(mailoutbox, booking)]
    assert subjects == [
        f"Your bank payment is processing: Booking #{booking.pk}",
        f"Your booking is confirmed: Booking #{booking.pk}",
    ]


def test_refund_receipt_is_emailed_with_its_pdf(client, committed, refunds, mailoutbox):
    booking = held_booking("200.00", caution_fee="50.00")
    with committed():
        pay(client, booking, Payment.Kind.FULL)
    mailoutbox.clear()

    with committed():
        stripe_service.refund_caution(booking, D("50.00"))

    [message] = to_customer(mailoutbox, booking)
    assert message.subject == f"Your refund: Booking #{booking.pk}"
    assert "caution refund of $50.00" in message.body
    receipt = documents(booking, Invoice.Kind.REFUND_RECEIPT)[0]
    assert [name for name, _, _ in message.attachments] == [f"{receipt.number}.pdf"]
    assert_common(message, booking, balance_link=False)


def test_emails_are_sent_only_after_commit(event_date, run_tasks, mailoutbox):
    with pytest.raises(RuntimeError), transaction.atomic():
        booking = online_booking(event_date, choice=PaymentChoice.ZELLE)
        zelle_payment(booking, "80.00")
        raise RuntimeError("rolled back")

    assert mailoutbox == []
    assert not SentEmail.objects.exists()


def test_emails_wait_for_the_commit(
    event_date, run_tasks, mailoutbox, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        online_booking(event_date, choice=PaymentChoice.ZELLE)
        assert mailoutbox == []

    assert mailoutbox == []
    with django_capture_on_commit_callbacks(execute=True):
        for callback in callbacks:
            callback()
    assert len(mailoutbox) == 2  # customer and owner


def test_each_email_is_recorded_and_never_sent_twice(committed, event_date, mailoutbox):
    with committed():
        booking = online_booking(event_date, choice=PaymentChoice.ZELLE)
    sent = len(mailoutbox)

    assert emails.send("booking_received", booking.pk) is False
    assert emails.send("owner_new_booking", booking.pk) is False

    assert len(mailoutbox) == sent
    keys = set(SentEmail.objects.values_list("key", flat=True))
    assert keys == {f"booking-received:{booking.pk}", f"owner-booking:{booking.pk}"}


def test_a_failed_send_is_not_recorded_and_the_retry_sends_it(event_date, mailoutbox):
    booking = online_booking(event_date, choice=PaymentChoice.ZELLE)
    failing = mock.patch(
        "django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("mail service down")
    )

    with failing, pytest.raises(OSError):
        emails.send("booking_received", booking.pk)
    assert not SentEmail.objects.exists()

    assert emails.send("booking_received", booking.pk) is True
    assert len(mailoutbox) == 1


def test_the_send_task_retries_when_the_mail_service_fails(event_date):
    from apps.notifications.tasks import send_email

    booking = online_booking(event_date, choice=PaymentChoice.ZELLE)
    real_send = mail.EmailMultiAlternatives.send
    calls = []

    def flaky(message, *args, **kwargs):
        calls.append(message.subject)
        if len(calls) < 3:
            raise OSError("temporary failure")
        return real_send(message, *args, **kwargs)

    with mock.patch.object(mail.EmailMultiAlternatives, "send", flaky):
        send_email.apply(args=["booking_received", booking.pk], throw=False)

    assert len(calls) == 3
    assert SentEmail.objects.filter(key=f"booking-received:{booking.pk}").count() == 1


def test_html_email_is_branded_and_mobile_friendly(committed, event_date, mailoutbox):
    with committed():
        online_booking(event_date, choice=PaymentChoice.ZELLE)

    body = html(to_customer(mailoutbox)[0])
    assert "https://rentals.example/static/brand/atglace-logo-header.png" in body
    assert "Montserrat,Arial,Helvetica,sans-serif" in body
    assert "#6a0dad" in body
    assert 'name="viewport"' in body
    assert "<!--[if mso]>" in body
    assert "max-width:600px" in body
