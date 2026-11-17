"""Scheduled reminders: each fires once, at the right time, and not when paid or cancelled."""

from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from django.conf import settings as django_settings
from django.utils import timezone

from apps.bookings.models import Booking
from apps.core import site_settings
from apps.notifications.models import Notification
from apps.notifications.services import messages, reminders
from apps.notifications.tests.helpers import (
    CUSTOMER_PHONE,
    deposit_booking,
    full_booking,
    manual_booking,
    rows,
)
from apps.payments.models import Payment
from apps.payments.services import ledger

pytestmark = pytest.mark.django_db
TODAY = date(2026, 11, 18)
OWNER_PHONE = "+14695550199"


@pytest.fixture
def committed(run_tasks, django_capture_on_commit_callbacks):
    def block():
        return django_capture_on_commit_callbacks(execute=True)

    return block


@pytest.fixture
def setup(twilio, clock, committed, settings, mailoutbox):
    """Twilio on, the owner's phone set, a review link, and noon on TODAY."""
    settings.OWNER_PHONE = OWNER_PHONE
    settings.REVIEW_URL = "https://g.page/r/atglace/review"
    clock.set(datetime(2026, 11, 18, 12, 0))

    def run(when):
        clock.set(when)
        with committed():
            return reminders.run()

    run.twilio = twilio
    run.outbox = mailoutbox
    run.clock = clock
    return run


def emails(outbox, kind_subject):
    return [message for message in outbox if message.subject.startswith(kind_subject)]


def on(day_offset, hour=10, minute=0):
    return datetime.combine(TODAY + timedelta(days=day_offset), datetime.min.time()).replace(
        hour=hour, minute=minute
    )


def switch_off(name):
    row = site_settings.get()
    setattr(row, name, False)
    row.save()


def cancel(booking):
    Booking.objects.filter(pk=booking.pk).update(status=Booking.Status.CANCELLED)


def pay_rest(booking):
    ledger.record_manual_payment(
        booking,
        method=Payment.Method.ZELLE,
        amount=booking.balance_due,
        received_at=timezone.now(),
        reference=f"ZL-rest-{booking.pk}",
        received_by=None,
    )


# Manual payment deadline: 4 hours before the hold ends.


def test_payment_deadline_reminder_4_hours_before_the_hold_ends(setup):
    booking = manual_booking(event_date=TODAY + timedelta(days=30))
    deadline = timezone.localtime(booking.hold_expires_at).replace(tzinfo=None)
    setup(deadline - timedelta(hours=4, minutes=15))
    assert setup.outbox == []
    setup(deadline - timedelta(hours=3, minutes=50))
    (email,) = emails(setup.outbox, "Your hold ends soon")
    assert "Zelle" in email.body and "/payments/manual/" in email.body
    (text,) = setup.twilio.to(CUSTOMER_PHONE)
    assert text["body"].startswith("Atglace Events: Reminder: your hold")
    setup(deadline - timedelta(hours=1))
    setup(deadline - timedelta(minutes=5))
    assert len(emails(setup.outbox, "Your hold ends soon")) == 1
    assert len(setup.twilio.sent) == 1


def test_no_payment_deadline_reminder_once_paid(setup):
    booking = manual_booking(event_date=TODAY + timedelta(days=30))
    deadline = timezone.localtime(booking.hold_expires_at).replace(tzinfo=None)
    ledger.record_manual_payment(
        booking,
        method=Payment.Method.ZELLE,
        amount=Decimal("100.00"),
        received_at=timezone.now(),
        reference="ZL-1",
        received_by=None,
    )
    setup(deadline - timedelta(hours=2))
    assert emails(setup.outbox, "Your hold ends soon") == []


def test_no_payment_deadline_reminder_when_cancelled_or_for_card_bookings(setup):
    cancelled = manual_booking(event_date=TODAY + timedelta(days=30))
    cancel(cancelled)
    card = manual_booking(event_date=TODAY + timedelta(days=30), payment_choice="stripe")
    deadline = timezone.localtime(card.hold_expires_at).replace(tzinfo=None)
    setup(deadline - timedelta(hours=2))
    assert setup.outbox == []


def test_an_extended_hold_gets_a_new_deadline_reminder(setup):
    booking = manual_booking(event_date=TODAY + timedelta(days=30))
    deadline = timezone.localtime(booking.hold_expires_at).replace(tzinfo=None)
    setup(deadline - timedelta(hours=2))
    Booking.objects.filter(pk=booking.pk).update(
        hold_expires_at=booking.hold_expires_at + timedelta(days=1)
    )
    setup(deadline + timedelta(days=1) - timedelta(hours=2))
    assert len(emails(setup.outbox, "Your hold ends soon")) == 2


# Balance due.


def test_balance_due_reminder_7_days_before_at_10_am(setup):
    booking = deposit_booking(event_date=TODAY + timedelta(days=7))
    setup(on(0, 9, 59))
    assert emails(setup.outbox, "Your balance is due") == []
    setup(on(0, 10, 0))
    (email,) = emails(setup.outbox, "Your balance is due")
    assert "/payments/balance/" in email.body
    assert "$100.00" in email.body
    (text,) = setup.twilio.to(CUSTOMER_PHONE)
    assert "/payments/balance/" in text["body"] and "$100.00" in text["body"]
    for day in range(0, 5):
        setup(on(day, 15))
    assert len(emails(setup.outbox, "Your balance is due")) == 1
    assert booking.notifications.filter(kind="balance_due").count() == 2


def test_balance_due_follows_the_setting(setup, settings):
    settings.BALANCE_DUE_DAYS_BEFORE = 10
    deposit_booking(event_date=TODAY + timedelta(days=10))
    setup(on(0, 11))
    assert len(emails(setup.outbox, "Your balance is due")) == 1


def test_no_balance_reminder_too_early(setup):
    deposit_booking(event_date=TODAY + timedelta(days=8))
    setup(on(0, 11))
    assert setup.outbox == []


def test_no_balance_reminders_when_paid_in_full(setup):
    full_booking(event_date=TODAY + timedelta(days=7))
    for day in range(0, 7):
        setup(on(day, 11))
    for subject in ("Your balance is due", "Reminder: your balance", "Unpaid balance"):
        assert emails(setup.outbox, subject) == []


def test_no_balance_reminders_when_cancelled(setup):
    booking = deposit_booking(event_date=TODAY + timedelta(days=7))
    cancel(booking)
    for day in range(0, 7):
        setup(on(day, 11))
    assert setup.outbox == []


def test_second_balance_reminder_2_days_before_if_still_unpaid(setup):
    deposit_booking(event_date=TODAY + timedelta(days=2))
    setup(on(0, 10, 15))
    (email,) = emails(setup.outbox, "Reminder: your balance is still due")
    assert "/payments/balance/" in email.body
    assert emails(setup.outbox, "Your balance is due") == []
    setup(on(0, 18))
    assert len(emails(setup.outbox, "Reminder: your balance is still due")) == 1


def test_second_balance_reminder_skipped_once_paid(setup):
    booking = deposit_booking(event_date=TODAY + timedelta(days=7))
    setup(on(0, 11))
    pay_rest(booking)
    setup(on(5, 11))
    assert emails(setup.outbox, "Reminder: your balance is still due") == []


def test_owner_alert_when_unpaid_1_day_before(setup):
    booking = deposit_booking(event_date=TODAY + timedelta(days=1))
    setup(on(0, 10, 0))
    (email,) = emails(setup.outbox, f"Unpaid balance on booking #{booking.pk}")
    assert email.to == [django_settings.BUSINESS_EMAIL]
    (text,) = setup.twilio.to(OWNER_PHONE)
    assert "unpaid" in text["body"] and "$100.00" in text["body"]
    setup(on(0, 16))
    assert len(setup.twilio.to(OWNER_PHONE)) == 1


def test_no_owner_unpaid_alert_when_paid(setup):
    full_booking(event_date=TODAY + timedelta(days=1))
    setup(on(0, 11))
    assert setup.twilio.to(OWNER_PHONE) == []


# Pre-event and thank-you.


def test_pre_event_reminder_the_day_before_at_10_am(setup):
    booking = full_booking(
        event_date=TODAY + timedelta(days=1),
        setup_by_time=datetime.min.time().replace(hour=14),
        delivery_address="12 Oak Lane\nPlano, TX 75024",
    )
    setup(on(0, 9, 45))
    assert emails(setup.outbox, "See you tomorrow") == []
    setup(on(0, 10, 0))
    (email,) = emails(setup.outbox, "See you tomorrow")
    for text in (
        "Thursday, November 19, 2026",
        "2:00 PM",
        "12 Oak Lane",
        "clear the setup area",
        "power outlet within reach for inflatables and machines",
        "pets away",
        django_settings.BUSINESS_PHONE,
    ):
        assert text in email.body
    (sms,) = setup.twilio.to(CUSTOMER_PHONE)
    assert "setup by 2:00 PM" in sms["body"] and "12 Oak Lane Plano, TX 75024" in sms["body"]
    setup(on(0, 20))
    assert len(emails(setup.outbox, "See you tomorrow")) == 1
    assert booking.notifications.filter(kind="pre_event", channel="sms").count() == 1


def test_no_pre_event_reminder_when_cancelled(setup):
    booking = full_booking(event_date=TODAY + timedelta(days=1))
    cancel(booking)
    setup(on(0, 11))
    assert setup.outbox == []


def test_thank_you_the_day_after_with_the_review_link(setup):
    full_booking(event_date=TODAY - timedelta(days=1))
    setup(on(0, 9))
    assert setup.outbox == []
    setup(on(0, 10))
    (email,) = emails(setup.outbox, "Thank you for renting with us")
    assert "https://g.page/r/atglace/review" in email.body
    (text,) = setup.twilio.to(CUSTOMER_PHONE)
    assert "https://g.page/r/atglace/review" in text["body"]
    setup(on(1, 11))
    assert len(emails(setup.outbox, "Thank you for renting with us")) == 1


def test_no_thank_you_without_a_review_link(setup, settings):
    settings.REVIEW_URL = ""
    full_booking(event_date=TODAY - timedelta(days=1))
    setup(on(0, 11))
    assert setup.outbox == []


def test_no_thank_you_for_a_cancelled_booking(setup):
    booking = full_booking(event_date=TODAY - timedelta(days=1))
    cancel(booking)
    setup(on(0, 11))
    assert setup.outbox == []


# Each reminder once over the whole lead-up, and the switches.


def test_each_reminder_fires_once_across_the_whole_lead_up(setup):
    booking = deposit_booking(event_date=TODAY + timedelta(days=9))
    start = on(0, 0)
    for hour in range(0, 24 * 11, 3):
        setup(start + timedelta(hours=hour))
    kinds = Counter(
        booking.notifications.filter(channel=Notification.Channel.EMAIL).values_list(
            "kind", flat=True
        )
    )
    assert kinds == {
        "balance_due": 1,
        "balance_due_final": 1,
        "owner_balance_unpaid": 1,
        "pre_event": 1,
        "thank_you": 1,
    }
    assert all(row.status == Notification.Status.SENT for row in rows(booking, channel="email"))


@pytest.mark.parametrize(
    ("kind", "event_offset", "make"),
    [
        ("balance_due", 7, deposit_booking),
        ("balance_due_final", 2, deposit_booking),
        ("owner_balance_unpaid", 1, deposit_booking),
        ("pre_event", 1, full_booking),
        ("thank_you", -1, full_booking),
    ],
)
def test_each_reminder_switch_turns_it_off(setup, kind, event_offset, make):
    booking = make(event_date=TODAY + timedelta(days=event_offset))
    switch_off(messages.REMINDER_SWITCHES[kind])
    setup(on(0, 11))
    assert not booking.notifications.filter(kind=kind).exists()


def test_payment_deadline_switch_turns_it_off(setup):
    booking = manual_booking(event_date=TODAY + timedelta(days=30))
    switch_off("reminder_payment_deadline")
    deadline = timezone.localtime(booking.hold_expires_at).replace(tzinfo=None)
    setup(deadline - timedelta(hours=2))
    assert setup.outbox == []


def test_reminder_emails_still_go_when_texts_are_off(setup, settings):
    settings.TWILIO_ACCOUNT_SID = ""
    deposit_booking(event_date=TODAY + timedelta(days=7))
    setup(on(0, 11))
    assert len(emails(setup.outbox, "Your balance is due")) == 1
    assert setup.twilio.sent == []


def test_reminder_paid_after_queueing_is_not_emailed(setup, run_tasks, monkeypatch):
    """A reminder whose email job runs after the balance is paid is skipped, not sent."""
    from apps.core import queue

    queued = []
    monkeypatch.setattr(queue, "enqueue", lambda task, *args: queued.append((task, args)))
    booking = deposit_booking(event_date=TODAY + timedelta(days=7))
    setup(on(0, 11))
    pay_rest(booking)
    for task, args in queued:
        task.apply(args=args, throw=True)
    assert emails(setup.outbox, "Your balance is due") == []
    (row,) = rows(booking, channel="email", kind="balance_due")
    assert row.status == Notification.Status.SKIPPED
