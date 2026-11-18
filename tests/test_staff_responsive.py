"""Browser checks for the staff screens: no sideways scrolling at 320px and 412px.

The dashboard, the crew schedule and the calendar are loaded as the owner and as crew with
very long names, addresses and notes. Needs the Python Playwright package and Chromium;
skipped with the reason shown where those are missing.
"""

import os
import re
from datetime import time, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.tests.factories import BookingItemFactory, ItemFactory, ItemVariantFactory
from apps.dashboard import roles
from apps.dashboard.tests.conftest import make_booking

sync_api = pytest.importorskip("playwright.sync_api", reason="Python Playwright is not installed")

pytestmark = pytest.mark.django_db(transaction=True)

WIDTHS = (320, 412)
LONG_NAME = "Maximiliana Alexandrina Featherstonehaugh-Worthington-Smythe the Third"
LONG_STREET = "12345 Extraordinarily Long Boulevard Northwest, Building C, Suite 4500, Gate 7"
S = Booking.Status


@pytest.fixture(scope="module")
def browser():
    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
    with sync_api.sync_playwright() as playwright:
        try:
            chromium = playwright.chromium.launch(args=["--disable-dev-shm-usage"])
        except sync_api.Error as error:  # pragma: no cover - depends on the machine
            pytest.skip(f"Chromium is not available: {error}")
        yield chromium
        chromium.close()


SMALL_TARGETS_SCRIPT = """() => [...document.querySelectorAll('a[href], button')]
  .filter(el => el.offsetParent !== null || getComputedStyle(el).position === 'fixed')
  .map(el => {
    const box = el.getBoundingClientRect();
    return [el.tagName, el.textContent.trim().slice(0, 30), Math.round(box.width),
            Math.round(box.height)];
  })
  .filter(([, , width, height]) => height < 44 && height > 0)"""


def session_for(user):
    client = Client()
    client.force_login(user)
    return client.cookies["sessionid"].value


@pytest.fixture
def staff_pages(live_server):
    """Owner and crew sessions, and a busy day full of long details."""
    user_model = get_user_model()
    owner = user_model.objects.create_superuser("amara", "a@example.com", "pw")
    crew = user_model.objects.create_user("tunde", password="pw", is_staff=True)  # noqa: S106
    group, _ = Group.objects.get_or_create(name=roles.CREW)
    group.permissions.set(
        Permission.objects.filter(
            content_type__app_label__in=[app for app, _ in roles.CREW_PERMISSIONS],
            codename__in=[code for _, code in roles.CREW_PERMISSIONS],
        )
    )
    crew.groups.add(group)
    today = timezone.localdate()
    for number in range(6):
        booking = make_booking(
            S.DEPOSIT_PAID if number % 2 else S.PAID,
            today,
            total="1234.50",
            paid="200.00",
            caution_fee="75.00",
            customer_name=LONG_NAME,
            email="maximiliana.alexandrina.featherstonehaugh@extraordinarily-long.example.com",
            phone="469-555-0142",
            delivery_address=f"{LONG_STREET}\nGrand Prairie, TX 75050",
            crew_notes="Please call when you are ten minutes away. " * 4,
            surface="grass",
            setup_by_time=time(10, 0),
            delivery_time=time(8, 30),
            pickup_date=today + timedelta(days=1),
            needs_attention=number == 0,
        )
        popcorn = ItemVariantFactory(
            item=ItemFactory(name="Extra large popcorn machine " * 2, needs_power=True)
        )
        BookingItemFactory(booking=booking, variant=popcorn, quantity=2)
    return live_server.url, session_for(owner), session_for(crew), today


def open_page(browser, base, session, width):
    context = browser.new_context(viewport={"width": width, "height": 800})
    host = re.sub(r"^https?://", "", base).split(":")[0]
    context.add_cookies([{"name": "sessionid", "value": session, "domain": host, "path": "/"}])
    return context, context.new_page()


@pytest.mark.parametrize("width", WIDTHS)
def test_dashboard_and_crew_schedule_do_not_scroll_sideways(browser, staff_pages, width):
    base, owner_session, crew_session, today = staff_pages
    crew_path = reverse("dashboard:crew_day", args=[today.isoformat()])
    checks = [
        (owner_session, reverse("admin:index")),
        (owner_session, reverse("dashboard:calendar")),
        (owner_session, crew_path),
        (crew_session, crew_path),
    ]
    wide = []
    for session, path in checks:
        context, page = open_page(browser, base, session, width)
        response = page.goto(base + path, wait_until="load")
        assert response.status < 400, (path, response.status)
        scroll_width = page.evaluate("document.documentElement.scrollWidth")
        if scroll_width > width:
            wide.append((path, scroll_width))
        context.close()
    assert wide == [], f"Staff pages wider than {width}px: {wide}"


@pytest.mark.parametrize("width", WIDTHS)
def test_tap_targets_are_at_least_44px_on_the_dashboard(browser, staff_pages, width):
    base, owner_session, _, _ = staff_pages
    context, page = open_page(browser, base, owner_session, width)
    page.goto(base + reverse("admin:index"), wait_until="load")
    small = page.evaluate(SMALL_TARGETS_SCRIPT)
    context.close()
    allowed = [row for row in small if row[0] != "INPUT"]
    assert allowed == [], allowed
