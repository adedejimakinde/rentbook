"""Calendar: month boundaries, week view, colour plus text label, and the HTMX partials."""

from datetime import date, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking
from apps.dashboard.services import calendar as calendar_service
from apps.dashboard.tests.conftest import make_booking

pytestmark = pytest.mark.django_db
S = Booking.Status


def test_month_grid_runs_sunday_to_saturday_and_marks_days_outside_the_month():
    page = calendar_service.build("month", date(2027, 2, 10))
    assert page.weeks[0][0].date == date(2027, 1, 31)
    assert page.weeks[-1][-1].date == date(2027, 3, 6)
    assert all(len(week) == 7 for week in page.weeks)
    assert [day.in_month for day in page.weeks[0]][:2] == [False, True]
    assert page.title == "February 2027"
    assert (page.previous, page.following) == (date(2027, 1, 1), date(2027, 3, 1))


def test_bookings_on_the_first_and_last_day_land_in_the_right_cells():
    first = make_booking(S.PAID, date(2027, 2, 1))
    last = make_booking(S.DEPOSIT_PAID, date(2027, 2, 28))
    spill = make_booking(S.PAID, date(2027, 3, 3))
    make_booking(S.PAID, date(2027, 3, 8))
    page = calendar_service.build("month", date(2027, 2, 15))
    by_date = {day.date: day for week in page.weeks for day in week}
    assert [e.booking.pk for e in by_date[date(2027, 2, 1)].entries] == [first.pk]
    assert [e.booking.pk for e in by_date[date(2027, 2, 28)].entries] == [last.pk]
    assert by_date[date(2027, 3, 3)].in_month is False
    assert [e.booking.pk for e in by_date[date(2027, 3, 3)].entries] == [spill.pk]
    assert date(2027, 3, 8) not in by_date


def test_december_rolls_into_january():
    last = make_booking(S.PAID, date(2028, 1, 1))
    page = calendar_service.build("month", date(2027, 12, 25))
    assert page.following == date(2028, 1, 1)
    end = page.weeks[-1][-1]
    assert (end.date, end.in_month) == (date(2028, 1, 1), False)
    assert [entry.booking.pk for entry in end.entries] == [last.pk]


def test_week_view_is_one_sunday_to_saturday_row():
    page = calendar_service.build("week", date(2027, 6, 16))
    (week,) = page.weeks
    assert (week[0].date, week[-1].date) == (date(2027, 6, 13), date(2027, 6, 19))
    assert page.previous == date(2027, 6, 6)
    assert page.following == date(2027, 6, 20)


def test_dead_bookings_are_not_shown():
    make_booking(S.CANCELLED, date(2027, 6, 12))
    make_booking(S.EXPIRED, date(2027, 6, 12))
    make_booking(S.HELD, date(2027, 6, 12), hold_expires_at=timezone.now() - timedelta(hours=1))
    live = make_booking(
        S.HELD, date(2027, 6, 12), hold_expires_at=timezone.now() + timedelta(hours=1)
    )
    page = calendar_service.build("month", date(2027, 6, 12))
    day = next(d for w in page.weeks for d in w if d.date == date(2027, 6, 12))
    assert [e.booking.pk for e in day.entries] == [live.pk]


def test_every_status_comes_with_a_text_label_not_just_a_colour(owner_client):
    make_booking(S.PAID, date(2027, 6, 12), customer_name="Paid Pat")
    make_booking(S.DEPOSIT_PAID, date(2027, 6, 12), customer_name="Deposit Dee")
    response = owner_client.get(
        reverse("dashboard:calendar"), {"date": "2027-06-12", "day": "2027-06-12"}
    )
    html = response.content.decode()
    assert "Paid in full" in html
    assert "Deposit paid" in html
    assert "Paid Pat" in html


def test_htmx_switches_return_only_the_body(owner_client):
    url = reverse("dashboard:calendar")
    page = owner_client.get(url, {"view": "week", "date": "2027-06-16"}, HTTP_HX_REQUEST="true")
    html = page.content.decode()
    assert "<html" not in html
    assert 'id="calendar-body"' in html
    assert "Jun 13 to Jun 19, 2027" in html
    assert len(page.content) < 15_000


def test_calendar_needs_the_dashboard_permission(client, crew, office):
    client.force_login(crew)
    assert client.get(reverse("dashboard:calendar")).status_code == 403
    client.force_login(office)
    assert client.get(reverse("dashboard:calendar")).status_code == 200


def test_anonymous_visitors_are_sent_to_the_admin_login(client):
    response = client.get(reverse("dashboard:calendar"))
    assert response.status_code == 302
    assert "login" in response["Location"]
