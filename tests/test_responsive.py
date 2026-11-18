"""Browser checks for the public site's responsive layout.

Every public page is loaded in Chromium at phone widths with a cart, a booking and very
long customer details, and must never scroll sideways. The floating WhatsApp button must
never sit on top of a control or a price, and the contact form's spam trap must stay
off screen.

These tests need the Python Playwright package and a Chromium build. They are skipped,
with the reason shown, where those are missing.
"""

import os
import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.services import links
from apps.bookings.tests.factories import (
    CategoryFactory,
    DeliveryZoneFactory,
    ItemFactory,
    ItemVariantFactory,
    PackageFactory,
    PackageItemFactory,
)
from apps.core.models import Review

sync_api = pytest.importorskip("playwright.sync_api", reason="Python Playwright is not installed")

pytestmark = pytest.mark.django_db(transaction=True)

OVERFLOW_WIDTHS = (320, 412)
LONG_NAME = "Maximiliana Alexandrina Featherstonehaugh-Worthington-Smythe the Third"
LONG_EMAIL = (
    "maximiliana.alexandrina.featherstonehaugh.worthington@"
    "extraordinarily-long-event-planning-company-domain.example.com"
)
LONG_STREET = "12345 Extraordinarily Long Boulevard Northwest, Building C, Suite 4500, Gate 7"
ZIP = "75204"


@pytest.fixture(scope="module")
def browser():
    # Playwright's sync API keeps an event loop on this thread; Django's database calls in
    # these tests are still synchronous, so allow them (test-only, this module only).
    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
    with sync_api.sync_playwright() as playwright:
        try:
            # A container's /dev/shm is small; let Chromium use /tmp instead.
            chromium = playwright.chromium.launch(args=["--disable-dev-shm-usage"])
        except sync_api.Error as error:  # pragma: no cover - depends on the machine
            pytest.skip(f"Chromium is not available: {error}")
        yield chromium
        chromium.close()
    os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)


@pytest.fixture
def site(live_server, settings):
    """A small catalogue, a zone, a review, a cart and a booking with very long details.

    Returns the live server URL, the session cookie of a visitor half-way through
    booking, and every public path to check.
    """
    settings.STRIPE_SECRET_KEY = "sk_test_responsive"  # noqa: S105
    settings.ZELLE_RECIPIENT = "pay@example.com"
    settings.ZELLE_NAME = "Atglace Events"
    category = CategoryFactory(name="Bounce houses and inflatables", slug="inflatables")
    items = []
    for number in range(6):
        item = ItemFactory(
            category=category,
            name=f"Extra large combo bounce house with slide number {number}",
            slug=f"bounce-{number}",
            short_description="Bounce area plus a slide in one unit, for mixed-age parties.",
            base_price=Decimal("1234.50"),
            caution_fee=Decimal("50.00"),
            is_featured=True,
        )
        items.append(ItemVariantFactory(item=item, stock_quantity=10))
    package = PackageFactory(name="Backyard birthday mega package", package_price=Decimal("499"))
    PackageItemFactory(package=package, variant=items[0], quantity=1)
    DeliveryZoneFactory(name="Dallas core", zip_codes=[ZIP], delivery_fee=Decimal("45.00"))
    Review.objects.create(
        customer_name=LONG_NAME,
        city="Grand Prairie",
        text="They delivered early, set everything up and the kids loved it. " * 3,
        rating=5,
        review_date=timezone.localdate(),
        is_published=True,
    )

    event_date = timezone.localdate() + timedelta(days=45)
    visitor = Client()
    steps = {
        "event": {
            "event_date": event_date.isoformat(),
            "start_time": "14:00",
            "setup_by_time": "12:30",
            "event_type": "birthday",
        },
        "delivery": {
            "street": LONG_STREET,
            "city": "Grand Prairie",
            "state": "TX",
            "zip_code": ZIP,
            "surface": "grass",
            "crew_notes": "Please call when you are ten minutes away. " * 4,
        },
        "contact": {"name": LONG_NAME, "email": LONG_EMAIL, "phone": "469-555-0100"},
    }

    def fill_cart_and_steps():
        for kind, pk, quantity in (("variant", items[0].pk, 2), ("package", package.pk, 1)):
            visitor.post(
                reverse("bookings:cart_add"), {"kind": kind, "id": pk, "quantity": quantity}
            )
        visitor.post(
            reverse("bookings:cart_details"), {"date": event_date.isoformat(), "zip_code": ZIP}
        )
        for step, data in steps.items():
            response = visitor.post(reverse("bookings:step", args=[step]), data)
            assert response.status_code == 302, (step, response.content.decode()[:500])

    # Book once with Zelle (a held booking with long details), then fill the cart again
    # so the cart and booking steps have something to show.
    fill_cart_and_steps()
    booked = visitor.post(
        reverse("bookings:step", args=["review"]),
        {"accept_terms": "on", "plan": "deposit", "choice": "zelle"},
    )
    assert booked.status_code == 302, booked.content.decode()[:500]
    manual_path = booked["Location"]
    booking = Booking.objects.latest("pk")
    fill_cart_and_steps()

    paths = [
        "/",
        "/rentals/",
        category.get_absolute_url(),
        items[0].item.get_absolute_url(),
        "/packages/",
        package.get_absolute_url(),
        "/gallery/",
        "/about/",
        "/faq/",
        "/contact/",
        "/links/",
        "/rental-terms/",
        "/privacy/",
        "/cart/",
        "/book/event/",
        "/book/delivery/",
        "/book/contact/",
        "/book/review/",
        manual_path,
        reverse("bookings:status", args=[links.make_token(booking, links.STATUS)]),
        reverse("payments:success", args=[links.make_token(booking, links.SUCCESS)]),
    ]
    booking.status = Booking.Status.DEPOSIT_PAID
    booking.save(update_fields=["status"])
    paths.append(reverse("payments:balance", args=[links.make_token(booking, links.BALANCE)]))
    return live_server.url, visitor.cookies["sessionid"].value, paths


def open_page(browser, base, session, width, height=800):
    context = browser.new_context(viewport={"width": width, "height": height})
    host = re.sub(r"^https?://", "", base).split(":")[0]
    context.add_cookies([{"name": "sessionid", "value": session, "domain": host, "path": "/"}])
    return context, context.new_page()


@pytest.mark.parametrize("width", OVERFLOW_WIDTHS)
def test_no_public_page_scrolls_sideways(browser, site, width):
    base, session, paths = site
    context, page = open_page(browser, base, session, width)
    wide = []
    for path in paths:
        response = page.goto(base + path, wait_until="load")
        assert response.status < 400, (path, response.status)
        scroll_width = page.evaluate("document.documentElement.scrollWidth")
        if scroll_width > width:
            wide.append((path, scroll_width))
    context.close()
    assert wide == [], f"Pages wider than {width}px: {wide}"


COVERED_SCRIPT = """() => {
  const fab = document.getElementById('fab-wa');
  if (!fab || getComputedStyle(fab).display === 'none' || fab.classList.contains('is-covering')
      || document.body.matches(':has(:is(input, select, textarea):focus)')) return [];
  const f = fab.getBoundingClientRect();
  const hits = [];
  for (const el of document.querySelectorAll(
      'input:not([type=hidden]), select, textarea, button, .btn, .price, .chip')) {
    if (fab.contains(el) || el.closest('.sticky-bar, .sticky-pill, .sr-only, .hp-field')) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || getComputedStyle(el).visibility === 'hidden') continue;
    if (r.left < f.right && r.right > f.left && r.top < f.bottom && r.bottom > f.top) {
      hits.push(el.outerHTML.slice(0, 80));
    }
  }
  return hits;
}"""


def test_floating_whatsapp_button_never_covers_controls_or_prices(browser, site):
    base, session, paths = site
    context, page = open_page(browser, base, session, 390, 844)
    covered = []
    for path in paths:
        page.goto(base + path, wait_until="load")
        height = page.evaluate("document.documentElement.scrollHeight")
        for top in range(0, height, 300):
            page.evaluate(f"window.scrollTo(0, {top})")
            page.wait_for_timeout(60)
            hits = page.evaluate(COVERED_SCRIPT)
            if hits:
                covered.append((path, top, hits))
    context.close()
    assert covered == [], covered[:5]


def test_contact_spam_trap_is_off_screen(browser, site):
    base, session, _ = site
    context, page = open_page(browser, base, session, 390)
    page.goto(base + "/contact/")
    box = page.locator('label[for="id_website"]').bounding_box()
    context.close()
    assert box is None or box["x"] + box["width"] <= 0
