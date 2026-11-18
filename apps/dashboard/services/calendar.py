"""Month and week calendars of bookings by event date."""

import calendar
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.utils import timezone

from apps.bookings.models import Booking
from apps.dashboard.services.money import with_money
from apps.dashboard.services.overview import is_live
from apps.dashboard.services.status import SCHEDULED_STATUSES, status_style

SUNDAY_FIRST = calendar.Calendar(firstweekday=6)


@dataclass
class CalendarEntry:
    booking: Booking
    label: str
    tone: str


@dataclass
class CalendarDay:
    date: object
    in_month: bool
    is_today: bool
    entries: list = field(default_factory=list)

    @property
    def tones(self):
        """Distinct status tones on this day, for the dots on a phone."""
        return list(dict.fromkeys(entry.tone for entry in self.entries))


@dataclass
class CalendarPage:
    view: str
    anchor: object
    weeks: list
    selected: object
    previous: object
    following: object
    title: str
    today: object

    @property
    def selected_day(self):
        for week in self.weeks:
            for day in week:
                if day.date == self.selected:
                    return day
        return None


def parse_anchor(value, today):
    """The date in value (YYYY-MM-DD), or today when missing or invalid."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return today


def _entries(start, end, now):
    bookings = with_money(
        Booking.objects.filter(status__in=SCHEDULED_STATUSES, event_date__range=(start, end))
    ).order_by("event_date", "pk")
    by_day = {}
    for booking in bookings:
        if is_live(booking, now):
            by_day.setdefault(booking.event_date, []).append(
                CalendarEntry(booking, *status_style(booking.status))
            )
    return by_day


def build(view, anchor, selected=None, now=None):
    """The calendar for view ("month" or "week") around the anchor date."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    if view == "week":
        start = anchor - timedelta(days=(anchor.weekday() + 1) % 7)
        days = [start + timedelta(days=offset) for offset in range(7)]
        weeks_of_days = [days]
        previous, following = start - timedelta(days=7), start + timedelta(days=7)
        title = f"{days[0]:%b %-d} to {days[-1]:%b %-d, %Y}"
    else:
        first = anchor.replace(day=1)
        weeks_of_days = SUNDAY_FIRST.monthdatescalendar(first.year, first.month)
        previous = (first - timedelta(days=1)).replace(day=1)
        following = (first + timedelta(days=32)).replace(day=1)
        title = f"{first:%B %Y}"
    first_day, last_day = weeks_of_days[0][0], weeks_of_days[-1][-1]
    by_day = _entries(first_day, last_day, now)
    weeks = [
        [
            CalendarDay(
                date=day,
                in_month=view == "week" or day.month == anchor.month,
                is_today=day == today,
                entries=by_day.get(day, []),
            )
            for day in week
        ]
        for week in weeks_of_days
    ]
    if selected is None:
        selected = today if first_day <= today <= last_day else anchor
    return CalendarPage(
        view=view,
        anchor=anchor,
        weeks=weeks,
        selected=selected,
        previous=previous,
        following=following,
        title=title,
        today=today,
    )
