"""Helpers for cart and booking flow tests: a rentable catalogue and the steps filled in."""

from decimal import Decimal

from django.urls import reverse

from apps.bookings.tests.factories import (
    DeliveryZoneFactory,
    ItemFactory,
    ItemVariantFactory,
    PackageFactory,
    PackageItemFactory,
)

ZIP = "75001"
HX = {"HX-Request": "true"}


def catalogue():
    """A served zone, a chair with a caution fee, and a package of two tables."""
    zone = DeliveryZoneFactory(
        name="Dallas", zip_codes=[ZIP], delivery_fee=Decimal("40.00"), setup_fee=Decimal("20.00")
    )
    chair = ItemVariantFactory(
        item=ItemFactory(name="Chair", base_price=Decimal("3.50"), caution_fee=Decimal("1.00")),
        stock_quantity=50,
    )
    table = ItemVariantFactory(
        item=ItemFactory(name="Table", base_price=Decimal("12.00")), stock_quantity=10
    )
    package = PackageFactory(name="Party set", package_price=Decimal("30.00"))
    PackageItemFactory(package=package, variant=table, quantity=2)
    return {"zone": zone, "chair": chair, "table": table, "package": package}


def add(client, kind, pk, quantity=1, htmx=True):
    return client.post(
        reverse("bookings:cart_add"),
        {"kind": kind, "id": pk, "quantity": quantity},
        headers=HX if htmx else {},
    )


def set_details(client, event_date, zip_code=ZIP, promo=""):
    return client.post(
        reverse("bookings:cart_details"),
        {"date": event_date.isoformat(), "zip_code": zip_code, "promo_code": promo},
        headers=HX,
    )


def event_data(event_date, **overrides):
    return {
        "event_date": event_date.isoformat(),
        "start_time": "14:00",
        "setup_by_time": "12:30",
        "event_type": "birthday",
        **overrides,
    }


def delivery_data(**overrides):
    return {
        "street": "1 Main St",
        "city": "Dallas",
        "state": "tx",
        "zip_code": ZIP,
        "surface": "grass",
        "crew_notes": "Gate code 1234",
        **overrides,
    }


def contact_data(**overrides):
    return {"name": "Ada Planner", "email": "ada@example.com", "phone": "469-555-0100", **overrides}


def step_url(step):
    return reverse("bookings:step", args=[step])


def fill_steps(client, event_date, contact=None):
    """Post the three detail steps, asserting each moves on."""
    for step, data in (
        ("event", event_data(event_date)),
        ("delivery", delivery_data()),
        ("contact", contact or contact_data()),
    ):
        response = client.post(step_url(step), data)
        assert response.status_code == 302, (step, response.content.decode()[:2000])


def ready_to_review(client, event_date, items):
    """A cart of 10 chairs and one package, with every detail step done."""
    add(client, "variant", items["chair"].pk, 10)
    add(client, "package", items["package"].pk, 1)
    set_details(client, event_date)
    fill_steps(client, event_date)


def review_data(**overrides):
    return {"accept_terms": "on", "plan": "deposit", "choice": "stripe", **overrides}
