"""Booking admin: the notification log, SMS consent, and resending the last reminder."""

from datetime import datetime, timedelta

import pytest
from django.urls import reverse

from apps.notifications.models import Notification
from apps.notifications.services import dispatch
from apps.notifications.tests.helpers import deposit_booking, rows

pytestmark = pytest.mark.django_db


@pytest.fixture
def committed(run_tasks, django_capture_on_commit_callbacks):
    def block():
        return django_capture_on_commit_callbacks(execute=True)

    return block


def change_page(admin_client, booking):
    url = reverse("admin:bookings_booking_change", args=[booking.pk])
    return admin_client.get(url).content.decode()


def resend(admin_client, *bookings):
    return admin_client.post(
        reverse("admin:bookings_booking_changelist"),
        {"action": "resend_last_reminder", "_selected_action": [b.pk for b in bookings]},
        follow=True,
    )


def test_booking_page_shows_the_log_and_sms_consent(admin_client, twilio, clock, committed):
    clock.set(datetime(2026, 11, 18, 12, 0))
    booking = deposit_booking(event_date=clock.now.date() + timedelta(days=7))
    with committed():
        dispatch.queue(booking, "balance_due")
    html = change_page(admin_client, booking)
    assert "Notification log" in html
    assert "balance_due" in html and "SMS" in html and "Sent" in html
    assert "Sms consent" in html


def test_resend_last_reminder_sends_it_again(admin_client, twilio, clock, committed, mailoutbox):
    clock.set(datetime(2026, 11, 18, 12, 0))
    booking = deposit_booking(event_date=clock.now.date() + timedelta(days=7))
    with committed():
        dispatch.queue(booking, "balance_due")
    with committed():
        response = resend(admin_client, booking)
    assert "Reminder sent again for booking" in response.content.decode()
    assert len([m for m in mailoutbox if m.subject.startswith("Your balance is due")]) == 2
    assert len(twilio.sent) == 2
    assert len(rows(booking, kind="balance_due", status=Notification.Status.SENT)) == 4


def test_resend_with_no_reminder_yet_warns(admin_client, twilio, clock):
    clock.set(datetime(2026, 11, 18, 12, 0))
    booking = deposit_booking(event_date=clock.now.date() + timedelta(days=7))
    response = resend(admin_client, booking)
    assert "No reminder to resend" in response.content.decode()


def test_resend_skips_a_reminder_that_no_longer_applies(admin_client, twilio, clock, committed):
    from apps.bookings.models import Booking

    clock.set(datetime(2026, 11, 18, 12, 0))
    booking = deposit_booking(event_date=clock.now.date() + timedelta(days=7))
    with committed():
        dispatch.queue(booking, "balance_due")
    Booking.objects.filter(pk=booking.pk).update(status=Booking.Status.CANCELLED)
    response = resend(admin_client, booking)
    assert "No reminder to resend" in response.content.decode()


def test_notification_log_admin_is_read_only(admin_client):
    url = reverse("admin:notifications_notification_changelist")
    assert admin_client.get(url).status_code == 200
    assert admin_client.get(reverse("admin:notifications_notification_add")).status_code == 403
