"""Shared site services: the visitor's chosen event date and ZIP, and contact email."""

import time
from datetime import date

from django.conf import settings
from django.core.mail import EmailMessage

from apps.bookings.models import normalise_zip
from apps.bookings.services.availability import notice_problem
from apps.bookings.services.quote import zone_for_zip

EVENT_DATE_KEY = "event_date"
ZIP_KEY = "event_zip"


def parse_date(text):
    """Parse a YYYY-MM-DD string from a date input, or return None."""
    try:
        return date.fromisoformat((text or "").strip())
    except ValueError:
        return None


def get_event_date(request):
    """The event date chosen earlier in this session, if it is still bookable."""
    chosen = parse_date(request.session.get(EVENT_DATE_KEY))
    if chosen is None or notice_problem(chosen):
        return None
    return chosen


def set_event_date(request, text):
    """Store the date from a form field in the session. Returns an error message or "".

    An empty value clears the stored date.
    """
    if not (text or "").strip():
        if EVENT_DATE_KEY in request.session:
            del request.session[EVENT_DATE_KEY]
        return ""
    chosen = parse_date(text)
    if chosen is None:
        return "Enter the event date as month, day and year."
    problem = notice_problem(chosen)
    if problem:
        return problem
    if request.session.get(EVENT_DATE_KEY) != chosen.isoformat():
        request.session[EVENT_DATE_KEY] = chosen.isoformat()
    return ""


def get_event_zip(request):
    """The delivery ZIP chosen earlier in this session, or ""."""
    return request.session.get(ZIP_KEY, "")


def quick_check(request, date_text, zip_text):
    """Validate an event date and delivery ZIP from the home page, remembering good values.

    Returns a dict with ``event_date``, ``zip_code``, ``zone`` (the DeliveryZone serving the
    ZIP, or None) and an error message for each part that failed.
    """
    date_error = set_event_date(request, date_text)
    if not (date_text or "").strip():
        date_error = "Choose your event date."
    zip_code = normalise_zip(zip_text)
    zip_error = "" if zip_code else "Enter a 5-digit ZIP code."
    zone = None
    if zip_code:
        if request.session.get(ZIP_KEY) != zip_code:
            request.session[ZIP_KEY] = zip_code
        zone = zone_for_zip(zip_code)
    return {
        "event_date": get_event_date(request) if not date_error else None,
        "date_error": date_error,
        "zip_code": zip_code,
        "zip_error": zip_error,
        "zone": zone,
    }


def contact_rate_limited(request, limit_per_hour):
    """Record a contact submission in the session; True when over the hourly limit."""
    now = time.time()
    recent = [t for t in request.session.get("contact_sent", []) if now - t < 3600]
    if len(recent) >= limit_per_hour:
        return True
    request.session["contact_sent"] = [*recent, now]
    return False


def send_contact_message(*, name, email, phone, event_date, message):
    """Email an enquiry from the contact form to the business, replying to the customer."""
    lines = [
        f"Name: {name}",
        f"Email: {email}",
        f"Phone: {phone or 'not given'}",
        f"Event date: {event_date or 'not given'}",
        "",
        message,
    ]
    EmailMessage(
        subject=f"Website enquiry from {name}",
        body="\n".join(lines),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[settings.BUSINESS_EMAIL],
        reply_to=[email],
    ).send()
