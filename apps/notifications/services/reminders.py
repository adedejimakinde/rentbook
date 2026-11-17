"""Scheduled reminders. run() is called every 15 minutes by Celery Beat.

Each reminder has a fixed key per booking, so running again (or twice at
once) never sends one twice. Dates are in the business time zone. Reminders
tied to a calendar day go out from 10 am on that day.
"""

import uuid
from datetime import time, timedelta

from django.conf import settings
from django.utils import timezone

from apps.bookings.models import MANUAL_PAYMENT_CHOICES, Booking
from apps.core import site_settings
from apps.notifications.services import dispatch, messages

DAY_REMINDER_TIME = time(10, 0)
PAYMENT_DEADLINE_HOURS = 4


def _days_left(booking, today):
    return (booking.event_date - today).days


def _send(booking, kind, suffix="", now=None):
    if not messages.still_applies(kind, booking, now):
        return False
    return dispatch.queue(booking, kind, suffix)


def payment_deadlines(now):
    """Held manual-payment bookings whose hold ends within 4 hours."""
    if not site_settings.reminder_enabled(messages.REMINDER_SWITCHES["payment_deadline"]):
        return 0
    bookings = Booking.objects.filter(
        status=Booking.Status.HELD,
        payment_choice__in=MANUAL_PAYMENT_CHOICES,
        hold_expires_at__gt=now,
        hold_expires_at__lte=now + timedelta(hours=PAYMENT_DEADLINE_HOURS),
    )
    sent = 0
    for booking in bookings:
        suffix = f"{booking.hold_expires_at:%Y%m%d%H%M}"
        sent += bool(_send(booking, "payment_deadline", suffix, now))
    return sent


def _upcoming(today, *, first, last):
    """Active bookings with an event from first to last days after today."""
    return Booking.objects.filter(
        status__in=messages.ACTIVE_STATUSES,
        event_date__gte=today + timedelta(days=first),
        event_date__lte=today + timedelta(days=last),
    )


def day_reminders(now):
    """Balance, owner, pre-event and thank-you reminders, from 10 am business time."""
    local = timezone.localtime(now)
    if local.time() < DAY_REMINDER_TIME:
        return 0
    today = local.date()
    sent = 0
    first_days = max(settings.BALANCE_DUE_DAYS_BEFORE, 3)
    plan = [
        ("balance_due", _upcoming(today, first=3, last=first_days)),
        ("balance_due_final", _upcoming(today, first=2, last=2)),
        ("owner_balance_unpaid", _upcoming(today, first=1, last=1)),
        ("pre_event", _upcoming(today, first=1, last=1)),
        (
            "thank_you",
            Booking.objects.filter(
                status__in=messages.FINISHED_STATUSES, event_date=today - timedelta(days=1)
            ),
        ),
    ]
    for kind, bookings in plan:
        if not site_settings.reminder_enabled(messages.REMINDER_SWITCHES[kind]):
            continue
        if kind == "balance_due":
            bookings = [
                b for b in bookings if _days_left(b, today) <= settings.BALANCE_DUE_DAYS_BEFORE
            ]
        for booking in bookings:
            sent += bool(_send(booking, kind, now=now))
    return sent


def run(now=None):
    """Queue every reminder that is due now. Returns how many were queued."""
    now = now or timezone.now()
    return payment_deadlines(now) + day_reminders(now)


def resend_last(booking):
    """Send the booking's latest reminder again, on every channel. Returns its kind, or None.

    Ignores the reminder's switch (staff asked for it) but still skips it when
    it no longer applies, and still honours consent, opt-outs and quiet hours.
    """
    last = (
        booking.notifications.filter(kind__in=messages.REMINDER_KINDS)
        .order_by("-created_at", "-pk")
        .first()
    )
    if last is None or not messages.still_applies(last.kind, booking):
        return None
    dispatch.queue(booking, last.kind, f"resend-{uuid.uuid4().hex[:12]}")
    return last.kind
