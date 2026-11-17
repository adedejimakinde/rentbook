"""SMS and WhatsApp: consent, STOP and START, quiet hours, templates, owner alerts, sent once."""

from datetime import date, datetime, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

from apps.bookings.models import Booking
from apps.core import site_settings
from apps.notifications.models import Notification, SmsOptOut
from apps.notifications.services import dispatch, events, sms
from apps.notifications.tests.helpers import CUSTOMER_PHONE, at, deposit_booking, rows

pytestmark = pytest.mark.django_db
OWNER_PHONE = "+14695550199"
INBOUND = "/notifications/twilio/inbound/"


@pytest.fixture
def daytime(clock):
    """Noon business time on a fixed day, well before the bookings' events."""
    clock.set(datetime(2026, 11, 18, 12, 0))
    return clock


@pytest.fixture
def committed(run_tasks, django_capture_on_commit_callbacks):
    def block():
        return django_capture_on_commit_callbacks(execute=True)

    return block


def booking(**kwargs):
    return deposit_booking(event_date=date(2026, 12, 20), **kwargs)


def send(committed, target, kind="pre_event", suffix=""):
    with committed():
        dispatch.queue(target, kind, suffix, email=False)


def test_sms_goes_to_customers_who_gave_consent(twilio, daytime, committed):
    target = booking()
    send(committed, target)
    assert len(twilio.to(CUSTOMER_PHONE)) == 1
    message = twilio.sent[0]
    assert message["body"].startswith("Atglace Events: ")
    assert message["from_"] == "+14695550000"
    (row,) = rows(target, channel="sms")
    assert row.status == Notification.Status.SENT
    assert row.provider_id.startswith("SM")


def test_no_sms_without_consent(twilio, daytime, committed):
    target = booking(consent=False)
    send(committed, target)
    assert twilio.sent == []
    (row,) = rows(target, channel="sms")
    assert row.status == Notification.Status.SKIPPED
    assert "consent" in row.error


def test_no_sms_when_twilio_is_not_configured(twilio, settings, daytime, committed):
    settings.TWILIO_AUTH_TOKEN = ""
    target = booking()
    send(committed, target)
    assert twilio.sent == []
    assert rows(target, channel="sms") == []


def test_no_sms_when_switched_off(twilio, daytime, committed):
    row = site_settings.get()
    row.sms_enabled = False
    row.save()
    target = booking()
    send(committed, target)
    assert twilio.sent == []
    assert rows(target, channel="sms") == []


def test_opted_out_number_never_gets_sms(twilio, daytime, committed):
    sms.opt_out("469-555-0101")
    target = booking()
    send(committed, target)
    assert twilio.sent == []
    assert rows(target, channel="sms")[0].error.startswith("Number opted out")


def test_opt_out_after_queueing_still_stops_the_message(twilio, daytime):
    target = booking()
    dispatch.queue(target, "pre_event", email=False)
    sms.opt_out(CUSTOMER_PHONE)
    (row,) = rows(target, channel="sms")
    assert dispatch.deliver(row.pk) == Notification.Status.SKIPPED
    assert twilio.sent == []


# STOP and START by Twilio webhook.


def signed_post(client, params, token="test-token"):  # noqa: S107
    url = f"http://testserver{INBOUND}"
    signature = RequestValidator(token).compute_signature(url, params)
    return client.post(INBOUND, params, HTTP_X_TWILIO_SIGNATURE=signature)


@pytest.mark.parametrize("word", ["STOP", "stop", " Unsubscribe ", "CANCEL", "END", "QUIT"])
def test_stop_words_opt_the_number_out(client, twilio, word):
    response = signed_post(client, {"From": CUSTOMER_PHONE, "Body": word})
    assert response.status_code == 200
    assert response["Content-Type"] == "text/xml"
    assert sms.is_opted_out(CUSTOMER_PHONE)


def test_start_opts_the_number_back_in(client, twilio, daytime, committed):
    signed_post(client, {"From": CUSTOMER_PHONE, "Body": "STOP"})
    signed_post(client, {"From": CUSTOMER_PHONE, "Body": "START"})
    row = SmsOptOut.objects.get(phone=CUSTOMER_PHONE)
    assert not row.opted_out and row.opted_in_at is not None
    send(committed, booking())
    assert len(twilio.sent) == 1


def test_other_replies_change_nothing(client, twilio):
    signed_post(client, {"From": CUSTOMER_PHONE, "Body": "Please stop by at 3"})
    assert not SmsOptOut.objects.exists()


def test_inbound_with_a_bad_signature_is_refused(client, twilio):
    response = signed_post(client, {"From": CUSTOMER_PHONE, "Body": "STOP"}, token="wrong")  # noqa: S106
    assert response.status_code == 403
    assert not SmsOptOut.objects.exists()


def test_inbound_without_a_signature_is_refused(client, twilio):
    response = client.post(INBOUND, {"From": CUSTOMER_PHONE, "Body": "STOP"})
    assert response.status_code == 403
    assert not SmsOptOut.objects.exists()


def test_inbound_is_refused_when_twilio_has_no_auth_token(client, twilio, settings):
    settings.TWILIO_AUTH_TOKEN = ""
    response = client.post(INBOUND, {"From": CUSTOMER_PHONE, "Body": "STOP"})
    assert response.status_code == 403


def test_inbound_url_name():
    assert reverse("notifications:twilio_inbound") == INBOUND


# Quiet hours: 9 pm to 8 am business time.


@pytest.mark.parametrize(
    ("local", "expected"),
    [
        (datetime(2026, 11, 18, 12, 0), datetime(2026, 11, 18, 12, 0)),
        (datetime(2026, 11, 18, 8, 0), datetime(2026, 11, 18, 8, 0)),
        (datetime(2026, 11, 18, 20, 59), datetime(2026, 11, 18, 20, 59)),
        (datetime(2026, 11, 18, 21, 0), datetime(2026, 11, 19, 8, 0)),
        (datetime(2026, 11, 18, 23, 30), datetime(2026, 11, 19, 8, 0)),
        (datetime(2026, 11, 19, 0, 15), datetime(2026, 11, 19, 8, 0)),
        (datetime(2026, 11, 19, 7, 59), datetime(2026, 11, 19, 8, 0)),
    ],
)
def test_next_send_time(local, expected):
    now = timezone.make_aware(local)
    assert dispatch.next_send_time(now) == timezone.make_aware(expected)


def test_texts_in_quiet_hours_wait_until_8_am(twilio, clock, committed, run_tasks):
    clock.set(datetime(2026, 11, 18, 12, 0))
    target = booking()
    clock.set(datetime(2026, 11, 18, 22, 30))
    send(committed, target)
    assert twilio.sent == []
    (row,) = rows(target, channel="sms")
    assert row.status == Notification.Status.SCHEDULED
    assert row.send_after == at(date(2026, 11, 19), 8)

    clock.set(datetime(2026, 11, 19, 7, 55))
    dispatch.send_due()
    assert twilio.sent == []

    clock.set(datetime(2026, 11, 19, 8, 0))
    assert dispatch.send_due() == 1
    assert len(twilio.sent) == 1
    row.refresh_from_db()
    assert row.status == Notification.Status.SENT
    assert dispatch.send_due() == 0
    assert len(twilio.sent) == 1


def test_emails_are_not_held_back_by_quiet_hours(twilio, clock, committed, mailoutbox):
    clock.set(datetime(2026, 11, 18, 12, 0))
    target = booking()
    clock.set(datetime(2026, 11, 18, 23, 0))
    with committed():
        dispatch.queue(target, "balance_due")
    assert len(mailoutbox) == 1
    assert twilio.sent == []


# WhatsApp.


@pytest.fixture
def whatsapp(settings, twilio):
    settings.WHATSAPP_ENABLED = True
    settings.TWILIO_WHATSAPP_FROM = "+14695550001"
    return twilio


def test_whatsapp_is_off_unless_enabled(twilio, daytime, committed, settings):
    settings.TWILIO_WHATSAPP_FROM = "+14695550001"
    settings.WHATSAPP_TEMPLATES = {"pre_event": "HX123"}
    target = booking()
    send(committed, target)
    assert rows(target, channel="whatsapp") == []
    assert all("whatsapp:" not in message["to"] for message in twilio.sent)


def test_whatsapp_without_a_template_is_skipped(whatsapp, daytime, committed):
    target = booking()
    send(committed, target)
    (row,) = rows(target, channel="whatsapp")
    assert row.status == Notification.Status.SKIPPED
    assert "template" in row.error
    assert [message for message in whatsapp.sent if "whatsapp:" in message["to"]] == []
    assert len(whatsapp.to(CUSTOMER_PHONE)) == 1


def test_whatsapp_uses_the_approved_template(whatsapp, daytime, committed, settings):
    settings.WHATSAPP_TEMPLATES = {"pre_event": "HX123"}
    target = booking()
    send(committed, target)
    (message,) = [m for m in whatsapp.sent if m["to"].startswith("whatsapp:")]
    assert message["to"] == f"whatsapp:{CUSTOMER_PHONE}"
    assert message["from_"] == "whatsapp:+14695550001"
    assert message["content_sid"] == "HX123"
    assert f'"2": "#{target.pk}"' in message["content_variables"]
    assert "body" not in message


def test_whatsapp_switch_off(whatsapp, daytime, committed, settings):
    settings.WHATSAPP_TEMPLATES = {"pre_event": "HX123"}
    row = site_settings.get()
    row.whatsapp_enabled = False
    row.save()
    target = booking()
    send(committed, target)
    assert rows(target, channel="whatsapp") == []


# Owner alerts.


def test_owner_is_texted_about_new_bookings(twilio, daytime, committed, settings):
    settings.OWNER_PHONE = "469 555 0199"
    target = booking(consent=False)
    with committed():
        events.queue_texts("owner_new_booking", target.pk)
    (message,) = twilio.to(OWNER_PHONE)
    assert f"New booking #{target.pk}" in message["body"]
    assert twilio.to(CUSTOMER_PHONE) == []


def test_owner_is_texted_about_payments(twilio, daytime, committed, settings, run_tasks):
    from apps.invoices.services import issuing

    settings.OWNER_PHONE = OWNER_PHONE
    target = booking()
    payment = target.payments.get()
    receipt, _ = issuing.issue_receipt(payment.pk)
    with committed():
        events.queue_texts("owner_payment", receipt.pk)
    (message,) = twilio.to(OWNER_PHONE)
    assert "Payment of $100.00" in message["body"]


def test_no_owner_alert_without_owner_phone(twilio, daytime, committed):
    target = booking(consent=False)
    with committed():
        events.queue_texts("owner_new_booking", target.pk)
    assert twilio.sent == []


def test_no_owner_alert_when_switched_off(twilio, daytime, committed, settings):
    settings.OWNER_PHONE = OWNER_PHONE
    row = site_settings.get()
    row.owner_alerts_enabled = False
    row.save()
    target = booking(consent=False)
    with committed():
        events.queue_texts("owner_new_booking", target.pk)
    assert twilio.sent == []


def test_new_online_booking_texts_owner_through_the_hooks(twilio, daytime, committed, settings):
    from apps.notifications import hooks

    settings.OWNER_PHONE = OWNER_PHONE
    target = booking()
    with committed():
        hooks.owner_new_booking(target.pk)
    assert len(twilio.to(OWNER_PHONE)) == 1


# Sent once.


def test_queueing_twice_sends_once(twilio, daytime, committed):
    target = booking()
    send(committed, target)
    send(committed, target)
    assert len(twilio.sent) == 1
    assert len(rows(target, channel="sms")) == 1


def test_delivering_twice_sends_once(twilio, daytime):
    target = booking()
    dispatch.queue(target, "pre_event", email=False)
    (row,) = rows(target, channel="sms")
    dispatch.deliver(row.pk)
    dispatch.deliver(row.pk)
    assert len(twilio.sent) == 1


def test_a_temporary_twilio_failure_is_retried_and_sent_once(twilio, daytime):
    target = booking()
    dispatch.queue(target, "pre_event", email=False)
    (row,) = rows(target, channel="sms")
    twilio.fail_with = TwilioRestException(500, "/Messages", "Service unavailable", code=20500)
    with pytest.raises(sms.SendError):
        dispatch.deliver(row.pk)
    row.refresh_from_db()
    assert row.status == Notification.Status.PENDING
    assert row.attempts == 1 and "20500" in row.error
    twilio.fail_with = None
    dispatch.deliver(row.pk)
    dispatch.deliver(row.pk)
    row.refresh_from_db()
    assert row.status == Notification.Status.SENT
    assert len(twilio.sent) == 1


def test_gives_up_after_max_attempts(twilio, daytime):
    target = booking()
    dispatch.queue(target, "pre_event", email=False)
    (row,) = rows(target, channel="sms")
    twilio.fail_with = TwilioRestException(500, "/Messages", "Down", code=20500)
    for _ in range(dispatch.MAX_ATTEMPTS - 1):
        with pytest.raises(sms.SendError):
            dispatch.deliver(row.pk)
    assert dispatch.deliver(row.pk) == Notification.Status.FAILED


def test_unsubscribed_error_fails_and_records_the_opt_out(twilio, daytime):
    target = booking()
    dispatch.queue(target, "pre_event", email=False)
    (row,) = rows(target, channel="sms")
    twilio.fail_with = TwilioRestException(400, "/Messages", "Unsubscribed", code=21610)
    assert dispatch.deliver(row.pk) == Notification.Status.FAILED
    assert sms.is_opted_out(CUSTOMER_PHONE)


def test_lost_pending_texts_are_sent_by_the_sweep(twilio, clock, run_tasks):
    clock.set(datetime(2026, 11, 18, 12, 0))
    target = booking()
    dispatch.queue(target, "pre_event", email=False)  # never committed: its job is lost
    clock.set(datetime(2026, 11, 18, 12, 5))
    assert dispatch.send_due() == 0
    clock.set(datetime(2026, 11, 18, 12, 0) + dispatch.LOST_AFTER + timedelta(minutes=1))
    assert dispatch.send_due() == 1
    assert len(twilio.sent) == 1


def test_a_cancelled_booking_is_not_texted(twilio, daytime):
    target = booking()
    dispatch.queue(target, "pre_event", email=False)
    Booking.objects.filter(pk=target.pk).update(status=Booking.Status.CANCELLED)
    (row,) = rows(target, channel="sms")
    assert dispatch.deliver(row.pk) == Notification.Status.SKIPPED
    assert twilio.sent == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(469) 555-0101", "+14695550101"),
        ("1-469-555-0101", "+14695550101"),
        ("whatsapp:+14695550101", "+14695550101"),
        ("+447700900123", "+447700900123"),
        ("555-0100", ""),
        ("", ""),
    ],
)
def test_normalise_phone(raw, expected):
    assert sms.normalise_phone(raw) == expected
