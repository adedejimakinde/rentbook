"""Shared site services: the visitor's chosen event date and ZIP, and contact email."""

import time
from datetime import date

from django.conf import settings
from django.core.cache import cache
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


SECTION_FLAGS_KEY = "site_section_flags"
SECTION_FLAGS_SECONDS = 300


def section_flags():
    """{"show_packages", "show_gallery"}: whether those sections have anything to show.

    Cached so the header and footer cost no queries on most requests; the
    catalogue signals clear the cache when packages or photos change.
    """
    flags = cache.get(SECTION_FLAGS_KEY)
    if flags is None:
        from apps.catalogue.models import ItemImage, Package

        flags = {
            "show_packages": Package.objects.filter(is_active=True, items__isnull=False).exists(),
            "show_gallery": ItemImage.objects.filter(renditions_ready=True).exists(),
        }
        cache.set(SECTION_FLAGS_KEY, flags, SECTION_FLAGS_SECONDS)
    return flags


def clear_section_flags():
    """Forget the cached section flags, so the next page recomputes them."""
    cache.delete(SECTION_FLAGS_KEY)


# Cities in the DFW delivery area, shown in the home strip and the footer.
SERVICE_CITIES = [
    "Dallas",
    "Fort Worth",
    "Plano",
    "Frisco",
    "Arlington",
    "Garland",
    "Irving",
    "Mesquite",
    "Forney",
    "Rockwall",
    "McKinney",
    "Grand Prairie",
]

# Events we set up, for the home page chips.
EVENT_TYPES = [
    "Birthdays",
    "Weddings",
    "Baby showers",
    "Graduations",
    "Church events",
    "School field days",
    "Corporate picnics",
    "Holiday parties",
]

# Home hero tabs. Each points at a category slug; the tab uses that category's photo and
# link when the category exists, and the full catalogue otherwise.
EVENT_TABS = [
    {
        "key": "birthdays",
        "label": "Birthdays",
        "eyebrow": "Backyard birthdays",
        "title": "Big party. Zero hauling.",
        "text": "Bounce houses, combos and treats delivered, set up and picked up across DFW.",
        "cta": "Shop inflatables",
        "category": "bounce-houses-and-inflatables",
    },
    {
        "key": "weddings",
        "label": "Weddings",
        "eyebrow": "Weddings and receptions",
        "title": "Seat every guest beautifully.",
        "text": "Chiavari chairs, round tables, linen and backdrops, set up before you arrive.",
        "cta": "Shop chairs",
        "category": "chairs",
    },
    {
        "key": "church-school",
        "label": "Church & school",
        "eyebrow": "Church and school events",
        "title": "Fun days made easy.",
        "text": "Inflatables, canopies, tables and chairs for fairs, field days and picnics.",
        "cta": "Shop tables",
        "category": "tables",
    },
    {
        "key": "concessions",
        "label": "Concessions",
        "eyebrow": "Treats and concessions",
        "title": "Popcorn and cotton candy, on us to set up.",
        "text": "Concession machines delivered ready to run, with supplies on request.",
        "cta": "Shop concessions",
        "category": "concessions",
    },
]


def event_tabs(categories):
    """The hero tabs with each tab's link and photo taken from the given categories."""
    by_slug = {category.slug: category for category in categories}
    tabs = []
    for tab in EVENT_TABS:
        category = by_slug.get(tab["category"])
        image = category if category is not None and category.renditions_ready else None
        tabs.append(
            {
                **tab,
                "url": category.get_absolute_url() if category else "",
                "image": image,
                "image_name": category.name if category else tab["label"],
            }
        )
    return tabs


def published_reviews(limit=12):
    """Published customer reviews, newest first. Empty when staff have published none."""
    from apps.core.models import Review

    return list(Review.objects.filter(is_published=True)[:limit])
