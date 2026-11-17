"""What each text message says, who it is for, and whether it still applies.

Every SMS starts with "Atglace Events:" so the customer knows who it is from.
WhatsApp templates get four values: {{1}} name, {{2}} booking reference,
{{3}} event date and {{4}} a link.
"""

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import MANUAL_PAYMENT_CHOICES, Booking, PaymentChoice
from apps.bookings.services import links

SENDER = "Atglace Events"

CUSTOMER_KINDS = {
    "booking_received",
    "booking_confirmed",
    "payment_deadline",
    "balance_due",
    "balance_due_final",
    "pre_event",
    "thank_you",
}
OWNER_KINDS = {"owner_new_booking", "owner_payment", "owner_balance_unpaid"}

# Each scheduled reminder and the SiteSettings switch that turns it on.
REMINDER_SWITCHES = {
    "payment_deadline": "reminder_payment_deadline",
    "balance_due": "reminder_balance_due",
    "balance_due_final": "reminder_balance_final",
    "owner_balance_unpaid": "reminder_owner_unpaid",
    "pre_event": "reminder_pre_event",
    "thank_you": "reminder_thank_you",
}
REMINDER_KINDS = set(REMINDER_SWITCHES)

# Bookings whose event is going ahead with money paid on it.
ACTIVE_STATUSES = (Booking.Status.DEPOSIT_PAID, Booking.Status.PAID, Booking.Status.DELIVERED)
FINISHED_STATUSES = (*ACTIVE_STATUSES, Booking.Status.RETURNED)

PREPARE = (
    "please clear the setup area, have a power outlet within reach for inflatables and "
    "machines, and keep pets away"
)


def is_owner_kind(kind):
    return kind in OWNER_KINDS


def still_applies(kind, booking, now=None):
    """False when the message no longer makes sense (paid, cancelled, hold over)."""
    now = now or timezone.now()
    if kind in ("booking_received", "payment_deadline"):
        return (
            booking.status == Booking.Status.HELD
            and booking.payment_choice in MANUAL_PAYMENT_CHOICES
            and booking.hold_expires_at is not None
            and booking.hold_expires_at > now
        )
    if kind in ("balance_due", "balance_due_final", "owner_balance_unpaid"):
        return booking.status in ACTIVE_STATUSES and booking.balance_due > 0
    if kind == "pre_event":
        return booking.status in ACTIVE_STATUSES
    if kind == "thank_you":
        return booking.status in FINISHED_STATUSES and bool(settings.REVIEW_URL)
    return booking.status not in (Booking.Status.CANCELLED, Booking.Status.EXPIRED)


def _url(path):
    return f"{settings.SITE_URL}{path}"


def status_url(booking):
    return _url(reverse("bookings:status", args=[links.make_token(booking, links.STATUS)]))


def balance_url(booking):
    return _url(reverse("payments:balance", args=[links.make_token(booking, links.BALANCE)]))


def manual_url(booking):
    return _url(reverse("payments:manual", args=[links.make_token(booking, links.MANUAL)]))


def admin_url(booking):
    return _url(reverse("admin:bookings_booking_change", args=[booking.pk]))


def _date(booking):
    return f"{booking.event_date:%a, %b} {booking.event_date.day}"


def _time(value):
    return value.strftime("%I:%M %p").lstrip("0") if value else ""


def _hold_end(booking):
    local = timezone.localtime(booking.hold_expires_at)
    return f"{_time(local.time())} on {local:%b} {local.day}"


def _method(booking):
    return PaymentChoice(booking.payment_choice).label if booking.payment_choice else "your"


def _money(amount):
    return f"${amount:,.2f}"


def build(kind, booking, extra=None):
    """(SMS body, WhatsApp template values) for kind about booking."""
    extra = extra or {}
    ref = f"#{booking.pk}"
    first_name = (booking.customer_name or "").split(" ")[0]
    when = _date(booking)
    link = status_url(booking)
    if kind == "booking_received":
        link = manual_url(booking)
        text = (
            f"We received booking {ref} for {when}. Please send your {_method(booking)} "
            f"payment by {_hold_end(booking)} to keep your items. How to pay: {link}"
        )
    elif kind == "booking_confirmed":
        text = f"Booking {ref} for {when} is confirmed. Thank you! Details: {link}"
    elif kind == "payment_deadline":
        link = manual_url(booking)
        text = (
            f"Reminder: your hold on booking {ref} ends at {_hold_end(booking)}. Send your "
            f"{_method(booking)} payment to keep your items: {link}"
        )
    elif kind == "balance_due":
        link = balance_url(booking)
        text = (
            f"The balance of {_money(booking.balance_due)} on booking {ref} ({when}) is due "
            f"before your event. Pay here: {link}"
        )
    elif kind == "balance_due_final":
        link = balance_url(booking)
        text = (
            f"Reminder: {_money(booking.balance_due)} is still due on booking {ref} for "
            f"{when}. Please pay today: {link}"
        )
    elif kind == "pre_event":
        setup = f", setup by {_time(booking.setup_by_time)}" if booking.setup_by_time else ""
        address = " ".join(booking.delivery_address.split())
        text = (
            f"See you tomorrow! Booking {ref}{setup} at {address}. Before we arrive, "
            f"{PREPARE}. Questions? Call {settings.BUSINESS_PHONE}."
        )
    elif kind == "thank_you":
        link = settings.REVIEW_URL
        text = f"Thank you for renting with us, {first_name}! We would love a review: {link}"
    elif kind == "owner_new_booking":
        link = admin_url(booking)
        text = (
            f"New booking {ref}: {booking.customer_name}, {when}, "
            f"{_money(extra.get('total', booking.total))}. {link}"
        )
    elif kind == "owner_payment":
        link = admin_url(booking)
        amount = extra.get("amount", "")
        text = f"Payment of ${amount} on booking {ref} ({booking.customer_name}). {link}"
    elif kind == "owner_balance_unpaid":
        link = admin_url(booking)
        text = (
            f"Balance of {_money(booking.balance_due)} unpaid on booking {ref} "
            f"({booking.customer_name}, {booking.phone}), event tomorrow. {link}"
        )
    else:
        raise ValueError(f"Unknown message kind {kind!r}")
    name = "Owner" if is_owner_kind(kind) else first_name
    variables = {"1": name, "2": ref, "3": when, "4": link}
    return f"{SENDER}: {text}", variables
