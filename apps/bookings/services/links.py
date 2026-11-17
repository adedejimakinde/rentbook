"""Signed links to one booking, for customers who have no account.

Each link carries the booking id and its own expiry, signed with SECRET_KEY
and a salt for its purpose, so a link made for one page cannot open another
and any change to it is refused.
"""

from datetime import datetime, time, timedelta

from django.conf import settings
from django.core import signing
from django.utils import timezone

from apps.bookings.models import Booking

SUCCESS = "success"
STATUS = "status"
BALANCE = "balance"
MANUAL = "manual"
PURPOSES = (SUCCESS, STATUS, BALANCE, MANUAL)

# The pages shown straight after booking only need to work for a while.
SHORT_LINK_DAYS = 7


class BadLink(Exception):
    """The link was changed, made for another page, or points at no booking."""


class ExpiredLink(BadLink):
    """The link was genuine but has passed its expiry."""


def _salt(purpose):
    if purpose not in PURPOSES:
        raise ValueError(f"Unknown link purpose {purpose!r}.")
    return f"rentbook.booking-link.{purpose}"


def end_of_day(day):
    """The last moment of a date in the business's time zone."""
    return timezone.make_aware(datetime.combine(day, time.max))


def expiry_for(booking, purpose, now=None):
    """When a link of this purpose made now stops working."""
    now = now or timezone.now()
    if purpose == BALANCE:
        return max(end_of_day(booking.event_date), now + timedelta(days=settings.BALANCE_LINK_DAYS))
    if purpose == STATUS:
        return now + timedelta(days=settings.STATUS_LINK_DAYS)
    return now + timedelta(days=SHORT_LINK_DAYS)


def make_token(booking, purpose, now=None):
    """A URL-safe signed token for the booking, valid for this purpose only."""
    expires = expiry_for(booking, purpose, now)
    return signing.dumps({"b": booking.pk, "e": int(expires.timestamp())}, salt=_salt(purpose))


def read_token(token, purpose, now=None):
    """Return the booking a token points at, or raise BadLink or ExpiredLink."""
    try:
        payload = signing.loads(token, salt=_salt(purpose))
        booking_id, expires = int(payload["b"]), int(payload["e"])
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        raise BadLink("This link is not valid.") from None
    now = now or timezone.now()
    if now.timestamp() > expires:
        raise ExpiredLink("This link has expired.")
    try:
        return Booking.objects.get(pk=booking_id)
    except Booking.DoesNotExist:
        raise BadLink("This link is not valid.") from None
