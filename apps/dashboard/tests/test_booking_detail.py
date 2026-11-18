"""The booking page in sections, and the city on the dashboard, crew schedule and list."""

import re
from datetime import date

import pytest
from django.contrib import admin
from django.urls import reverse

from apps.bookings.admin import BOOKING_FIELDSETS
from apps.bookings.models import Booking
from apps.bookings.tests.factories import DeliveryZoneFactory
from apps.dashboard.services import crew as crew_service
from apps.dashboard.services import overview
from apps.dashboard.tests.conftest import make_booking

pytestmark = pytest.mark.django_db
S = Booking.Status
SECTIONS = ["Customer", "Event", "Delivery and crew", "Money and payments", "Notifications"]


def headings(html):
    return re.findall(r"<h2[^>]*>\s*([^<]{3,40}?)\s*</h2>", html)


def test_the_booking_page_has_the_five_sections_in_order(owner_client):
    booking = make_booking(S.PAID)
    page = owner_client.get(reverse("admin:bookings_booking_change", args=[booking.pk]))
    found = [h for h in headings(page.content.decode()) if h in SECTIONS]
    assert found == SECTIONS
    assert [title for title, _ in BOOKING_FIELDSETS] == SECTIONS


def test_every_booking_field_is_in_exactly_one_section():
    ma = admin.site._registry[Booking]
    in_sections = [name for _, options in BOOKING_FIELDSETS for name in options["fields"]]
    assert len(in_sections) == len(set(in_sections))
    model_fields = {
        f.name for f in Booking._meta.fields if f.editable and not f.auto_created and f.name != "id"
    }
    unplaced = (model_fields | set(ma.readonly_fields) | set(ma.money_fields)) - set(in_sections)
    assert unplaced == set()
    assert set(in_sections) <= model_fields | set(ma.readonly_fields) | set(ma.money_fields)


def test_the_add_page_still_works_and_skips_fields_that_need_a_saved_booking(owner_client):
    page = owner_client.get(reverse("admin:bookings_booking_add"))
    assert page.status_code == 200
    html = page.content.decode()
    assert "Customer" in html and "Delivery and crew" in html
    assert "Amount paid" not in html


def test_crew_see_the_sections_without_money_or_notification_fields_they_may_not_have(crew_client):
    booking = make_booking(S.PAID, total="321.00", caution_fee="45.00", caution_paid="45.00")
    html = crew_client.get(
        reverse("admin:bookings_booking_change", args=[booking.pk])
    ).content.decode()
    found = [h for h in headings(html) if h in SECTIONS]
    assert "Customer" in found and "Delivery and crew" in found
    assert "Money and payments" not in found
    for text in ("321.00", "Amount paid", "Unit price", "Invoices and receipts"):
        assert text not in html


def test_read_only_rules_still_hold_with_sections(owner_client):
    booking = make_booking(S.DEPOSIT_PAID, total="300.00", paid="100.00")
    html = owner_client.get(
        reverse("admin:bookings_booking_change", args=[booking.pk])
    ).content.decode()
    assert re.search(r'name="(tax|caution_fee|delivery_fee|status)"', html) is None


def test_the_city_is_editable_on_the_booking(owner_client):
    booking = make_booking(S.QUOTED, city="Garland")
    html = owner_client.get(
        reverse("admin:bookings_booking_change", args=[booking.pk])
    ).content.decode()
    assert 'name="city"' in html
    assert "Garland" in html


# City -------------------------------------------------------------------------


def test_dashboard_jobs_use_the_saved_city_then_the_zone(today):
    DeliveryZoneFactory(name="Collin zone", zip_codes=["75034"])
    make_booking(S.PAID, today, city="Garland", zip_code="75034", delivery_address="1 Elm St")
    make_booking(S.PAID, today, city="", zip_code="75034", delivery_address="2 Elm St, Frisco, TX")
    cities = sorted(job.city for job in overview.build().jobs if job.stop.is_delivery)
    assert cities == ["Collin zone", "Garland"]


def test_crew_cards_show_the_saved_city(owner):
    make_booking(S.PAID, date(2027, 6, 12), city="Garland", customer_name="Johnson")
    (card, *_) = crew_service.schedule(date(2027, 6, 12), owner)
    assert card.city == "Garland"


def test_the_booking_list_shows_and_searches_the_city(owner_client):
    garland = make_booking(S.PAID, city="Garland")
    make_booking(S.PAID, city="Plano")
    response = owner_client.get(reverse("admin:bookings_booking_changelist"), {"q": "garland"})
    assert {b.pk for b in response.context["cl"].result_list} == {garland.pk}
    assert "Garland" in response.content.decode()


# Sidebar ----------------------------------------------------------------------


def test_the_sidebar_has_a_sticky_inner_panel_inside_a_full_height_column(owner_client):
    html = owner_client.get(reverse("admin:index")).content.decode()
    assert '<aside class="side no-print"' in html
    assert '<div class="side-inner">' in html
    css = open("assets/css/source.css").read()
    inner = css[css.index(".side-inner {") :]
    assert "position: sticky" in inner[: inner.index("}")]
