"""Queue and deliver customer and owner messages on every active channel, once each.

Each message on each channel is one Notification row with a unique key, made
in the caller's transaction; its delivery job runs after commit. A retried or
repeated call finds the row already there and sends nothing more.

Email always goes out (through the email service). SMS goes to customers who
gave consent; WhatsApp only when it is switched on, configured, and the kind
has an approved template. No SMS or WhatsApp between 9 pm and 8 am business
time: those wait until 8 am.
"""

from datetime import datetime, time, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core import queue as jobs
from apps.core import site_settings
from apps.notifications.models import Notification
from apps.notifications.services import messages, sms

QUIET_START = time(21, 0)
QUIET_END = time(8, 0)
MAX_ATTEMPTS = 5
# A pending text whose job has not run after this long is queued again.
LOST_AFTER = timedelta(minutes=10)
TEXT_CHANNELS = (Notification.Channel.SMS, Notification.Channel.WHATSAPP)


def next_send_time(now=None):
    """now, or 8 am business time when now is in quiet hours (9 pm to 8 am)."""
    now = now or timezone.now()
    local = timezone.localtime(now)
    if QUIET_END <= local.time() < QUIET_START:
        return now
    day = local.date() if local.time() < QUIET_END else local.date() + timedelta(days=1)
    return timezone.make_aware(datetime.combine(day, QUIET_END))


def make_key(kind, booking, suffix=""):
    key = f"{kind}:{booking.pk}"
    return f"{key}:{suffix}" if suffix else key


def _row(booking, channel, kind, key, **fields):
    """Create the row for key on channel, or return None when it already exists."""
    try:
        with transaction.atomic():
            return Notification.objects.create(
                booking=booking, channel=channel, kind=kind, unique_key=f"{key}:{channel}", **fields
            )
    except IntegrityError:
        return None


def _deliver_after_commit(notification):
    from apps.notifications.tasks import deliver

    jobs.enqueue_on_commit(deliver, notification.pk)


def _text_recipient(kind, booking):
    """(phone, reason it cannot be texted or "")."""
    if messages.is_owner_kind(kind):
        if not site_settings.owner_alerts().active:
            return "", "owner alerts off"
        return sms.normalise_phone(settings.OWNER_PHONE), ""
    phone = sms.normalise_phone(booking.phone)
    if not booking.sms_consent:
        return phone, "No SMS consent on this booking."
    if not phone:
        return "", "Phone number is not a valid mobile number."
    return phone, ""


def queue_texts(booking, kind, key, extra=None):
    """Make the SMS and WhatsApp rows for one message and queue their delivery."""
    phone, reason = _text_recipient(kind, booking)
    if reason == "owner alerts off":
        return []
    body, variables = messages.build(kind, booking, extra)
    made = []
    channels = []
    if site_settings.sms().active:
        channels.append(Notification.Channel.SMS)
    if site_settings.whatsapp().active:
        channels.append(Notification.Channel.WHATSAPP)
    for channel in channels:
        problem = reason
        if not problem and sms.is_opted_out(phone):
            problem = "Number opted out (replied STOP)."
        if not problem and channel == Notification.Channel.WHATSAPP:
            if not settings.WHATSAPP_TEMPLATES.get(kind):
                problem = "No WhatsApp template configured for this message."
        row = _row(
            booking,
            channel,
            kind,
            key,
            recipient=phone,
            body=body,
            variables=variables,
            status=Notification.Status.SKIPPED if problem else Notification.Status.PENDING,
            error=problem,
        )
        if row is None:
            continue
        made.append(row)
        if not problem:
            _deliver_after_commit(row)
    return made


def queue_email(booking, kind, key, suffix=""):
    """Make the email row for a reminder and queue the email job. False if already done."""
    from apps.notifications.tasks import send_email

    recipient = settings.BUSINESS_EMAIL if messages.is_owner_kind(kind) else booking.email
    row = _row(booking, Notification.Channel.EMAIL, kind, key, recipient=recipient)
    if row is None:
        return False
    jobs.enqueue_on_commit(send_email, kind, booking.pk, suffix)
    return True


def queue(booking, kind, suffix="", *, email=True, extra=None):
    """Send kind about booking on email and every active text channel, once per key.

    Returns False when this message (its email, or texts with no email) was
    already queued, so nothing new was sent.
    """
    key = make_key(kind, booking, suffix)
    with transaction.atomic():
        queued = queue_email(booking, kind, key, suffix) if email else True
        if not queued:
            return False
        rows = queue_texts(booking, kind, key, extra)
    return email or bool(rows)


def record_email(email_key, kind, booking, recipient, status, error=""):
    """Log an email in the notification log (the email service calls this)."""
    Notification.objects.update_or_create(
        unique_key=f"{email_key}:{Notification.Channel.EMAIL}",
        defaults={
            "booking": booking,
            "channel": Notification.Channel.EMAIL,
            "kind": kind,
            "recipient": recipient,
            "status": status,
            "error": error,
            "sent_at": timezone.now() if status == Notification.Status.SENT else None,
        },
    )


def _channel_active(channel):
    if channel == Notification.Channel.SMS:
        return site_settings.sms().active
    return site_settings.whatsapp().active


def _skip(notification, reason):
    notification.status = Notification.Status.SKIPPED
    notification.error = reason
    notification.save(update_fields=["status", "error", "updated_at"])


def _why_not_now(notification, now):
    """Reason to skip this text now, or ""."""
    if not _channel_active(notification.channel):
        return f"{notification.get_channel_display()} is switched off or not configured."
    if sms.is_opted_out(notification.recipient):
        return "Number opted out (replied STOP)."
    booking = notification.booking
    owner = messages.is_owner_kind(notification.kind)
    if owner and not site_settings.owner_alerts().active:
        return "Owner alerts are switched off."
    if booking is not None and not owner and not booking.sms_consent:
        return "No SMS consent on this booking."
    if booking is not None and not messages.still_applies(notification.kind, booking, now):
        return "No longer needed (paid, cancelled or past)."
    return ""


def deliver(notification_id, now=None):
    """Send one queued text if it is still due. Returns the row's status afterwards.

    Holds the row lock while talking to Twilio, so two workers never send the
    same message. Raises sms.SendError on a temporary failure so the job is
    retried, up to MAX_ATTEMPTS.
    """
    now = now or timezone.now()
    retry = None
    with transaction.atomic():
        notification = (
            Notification.objects.select_for_update(of=("self",))
            .select_related("booking")
            .get(pk=notification_id)
        )
        if notification.status not in (Notification.Status.PENDING, Notification.Status.SCHEDULED):
            return notification.status
        if notification.send_after and notification.send_after > now:
            return notification.status
        reason = _why_not_now(notification, now)
        if reason:
            _skip(notification, reason)
            return notification.status
        allowed = next_send_time(now)
        if allowed > now:
            notification.status = Notification.Status.SCHEDULED
            notification.send_after = allowed
            notification.save(update_fields=["status", "send_after", "updated_at"])
            return notification.status
        notification.attempts += 1
        try:
            if notification.channel == Notification.Channel.SMS:
                provider_id = sms.send_sms(notification.recipient, notification.body)
            else:
                template = settings.WHATSAPP_TEMPLATES.get(notification.kind, "")
                if not template:
                    _skip(notification, "No WhatsApp template configured for this message.")
                    return notification.status
                provider_id = sms.send_whatsapp(
                    notification.recipient, template, notification.variables
                )
        except sms.SendError as error:
            notification.error = str(error)
            if error.code == sms.UNSUBSCRIBED_ERROR:
                sms.opt_out(notification.recipient, now)
            if error.permanent or notification.attempts >= MAX_ATTEMPTS:
                notification.status = Notification.Status.FAILED
            else:
                retry = error
            notification.save(update_fields=["status", "error", "attempts", "updated_at"])
        else:
            notification.status = Notification.Status.SENT
            notification.provider_id = provider_id or ""
            notification.sent_at = now
            notification.error = ""
            notification.save(
                update_fields=[
                    "status",
                    "provider_id",
                    "sent_at",
                    "error",
                    "attempts",
                    "updated_at",
                ]
            )
    if retry is not None:
        raise retry
    return notification.status


def send_due(now=None):
    """Queue texts whose quiet-hours wait is over, and pending texts whose job was lost."""
    from apps.notifications.tasks import deliver as deliver_task

    now = now or timezone.now()
    due = Notification.objects.filter(channel__in=TEXT_CHANNELS).filter(
        status=Notification.Status.SCHEDULED, send_after__lte=now
    ) | Notification.objects.filter(
        channel__in=TEXT_CHANNELS,
        status=Notification.Status.PENDING,
        updated_at__lte=now - LOST_AFTER,
    )
    ids = list(due.values_list("pk", flat=True))
    for notification_id in ids:
        jobs.enqueue(deliver_task, notification_id)
    return len(ids)
