"""Crew schedule: order and content, the run sheet, marking through the lifecycle, and roles."""

from datetime import date, time, timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.services import lifecycle
from apps.bookings.tests.factories import (
    BookingItemFactory,
    DeliveryZoneFactory,
    ItemFactory,
    ItemVariantFactory,
)
from apps.core import site_settings
from apps.dashboard.services import crew as crew_service
from apps.dashboard.services import runsheet
from apps.dashboard.tests.conftest import make_booking
from apps.payments.models import Payment

pytestmark = pytest.mark.django_db
S = Booking.Status
DAY = date(2027, 6, 12)
D = Decimal


def day_url(day=DAY):
    return reverse("dashboard:crew_day", args=[day.isoformat()])


def stop_order(user, day=DAY):
    return [(c.booking.customer_name, c.stop.kind) for c in crew_service.schedule(day, user)]


def test_stops_are_in_time_order_and_unset_times_come_last(owner):
    make_booking(
        S.PAID, DAY, customer_name="Late", delivery_time=time(15, 0), pickup_date=date(2027, 6, 13)
    )
    make_booking(
        S.PAID, DAY, customer_name="Early", delivery_time=time(8, 30), pickup_date=date(2027, 6, 13)
    )
    make_booking(
        S.PAID,
        DAY,
        customer_name="Unset B",
        setup_by_time=time(12, 0),
        pickup_date=date(2027, 6, 13),
    )
    make_booking(
        S.PAID,
        DAY,
        customer_name="Unset A",
        setup_by_time=time(9, 0),
        pickup_date=date(2027, 6, 13),
    )
    make_booking(
        S.DELIVERED,
        date(2027, 6, 11),
        customer_name="Pickup",
        pickup_date=DAY,
        pickup_time=time(9, 0),
    )
    assert stop_order(owner) == [
        ("Early", "delivery"),
        ("Pickup", "pickup"),
        ("Late", "delivery"),
        ("Unset A", "delivery"),
        ("Unset B", "delivery"),
    ]


def test_pickup_defaults_to_the_event_date_and_can_be_another_day(owner):
    same_day = make_booking(S.PAID, DAY, customer_name="Same day")
    next_morning = make_booking(
        S.DELIVERED, DAY, customer_name="Next morning", pickup_date=date(2027, 6, 13)
    )
    assert same_day.effective_pickup_date == DAY
    assert ("Same day", "pickup") in stop_order(owner)
    assert ("Next morning", "pickup") not in stop_order(owner)
    assert ("Next morning", "pickup") in stop_order(owner, date(2027, 6, 13))
    assert next_morning.effective_pickup_date == date(2027, 6, 13)


def test_cancelled_and_held_bookings_are_not_on_the_schedule(owner):
    make_booking(S.CANCELLED, DAY)
    make_booking(S.HELD, DAY, hold_expires_at=timezone.now() + timedelta(hours=2))
    assert stop_order(owner) == []


def test_a_card_has_everything_the_crew_needs(owner_client):
    DeliveryZoneFactory(name="Zone", zip_codes=["75040"])
    booking = make_booking(
        S.DEPOSIT_PAID,
        DAY,
        total="400.00",
        paid="150.00",
        caution_fee="50.00",
        customer_name="Johnson birthday",
        phone="(469) 555-0142",
        delivery_address="12 Elm St\nGarland, TX 75040",
        surface="grass",
        setup_by_time=time(11, 0),
        delivery_time=time(9, 30),
        crew_notes="Gate code 4411. Dog in yard.",
        pickup_date=date(2027, 6, 13),
    )
    machine = ItemVariantFactory(item=ItemFactory(name="Popcorn machine", needs_power=True))
    BookingItemFactory(booking=booking, variant=machine, quantity=2)
    html = owner_client.get(day_url()).content.decode()
    for text in (
        "Johnson birthday",
        "9:30 AM",
        "Garland",
        "google.com/maps/search/?api=1&amp;query=12+Elm+St+Garland%2C+TX+75040",
        'href="tel:4695550142"',
        "(469) 555-0142",
        "2 × Popcorn machine",
        "Grass",
        "11:00 AM",
        "Needs an outlet: Popcorn machine",
        "Gate code 4411. Dog in yard.",
        "Collect $300",
        "Mark delivered",
    ):
        assert text in html, text
    assert booking.pk


def test_day_navigation_and_empty_day(owner_client):
    html = owner_client.get(day_url()).content.decode()
    assert day_url(date(2027, 6, 11)) in html
    assert day_url(date(2027, 6, 13)) in html
    assert "No deliveries or pickups on this day." in html
    assert owner_client.get(reverse("dashboard:crew_day", args=["not-a-date"])).status_code == 404


def test_crew_today_redirects_to_the_dated_page(owner_client):
    response = owner_client.get(reverse("dashboard:crew"))
    assert response.status_code == 302
    assert "/crew/" in response["Location"]


# Run sheet -----------------------------------------------------------------


def test_run_sheet_pdf_renders(owner_client):
    make_booking(S.PAID, DAY, customer_name="Johnson birthday", pickup_date=date(2027, 6, 13))
    response = owner_client.get(reverse("dashboard:run_sheet", args=[DAY.isoformat()]))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert "run-sheet-2027-06-12.pdf" in response["Content-Disposition"]


def test_run_sheet_has_a_checklist_per_stop(owner):
    make_booking(
        S.PAID, DAY, customer_name="One", delivery_time=time(9, 0), pickup_date=date(2027, 6, 13)
    )
    make_booking(S.DELIVERED, date(2027, 6, 11), customer_name="Two", pickup_date=DAY)
    html = runsheet.render_html(DAY, owner)
    assert html.count('<ul class="check">') == 2
    assert "Photograph the finished setup" in html
    assert "Inspect each item for damage" in html
    from weasyprint import HTML

    assert len(HTML(string=html).render().pages) >= 1


# Marking -------------------------------------------------------------------


def post_mark(client, booking, action, kind, **extra):
    return client.post(
        reverse("dashboard:crew_mark", args=[booking.pk]),
        {"action": action, "kind": kind, "day": DAY.isoformat(), **extra},
    )


def test_mark_delivered_goes_through_the_lifecycle_service(owner_client, owner):
    booking = make_booking(S.PAID, DAY)
    with mock.patch.object(lifecycle, "mark_delivered", wraps=lifecycle.mark_delivered) as spy:
        response = post_mark(owner_client, booking, "delivered", "delivery")
    assert response.status_code == 302
    spy.assert_called_once()
    assert spy.call_args.kwargs == {"by": owner, "override_note": ""}
    booking.refresh_from_db()
    assert booking.status == S.DELIVERED


def test_mark_returned_goes_through_the_lifecycle_service(owner_client):
    booking = make_booking(S.DELIVERED, DAY)
    with mock.patch.object(lifecycle, "mark_returned", wraps=lifecycle.mark_returned) as spy:
        post_mark(owner_client, booking, "returned", "pickup")
    spy.assert_called_once()
    booking.refresh_from_db()
    assert booking.status == S.RETURNED


def test_the_views_never_change_a_booking_themselves(owner_client):
    """With the lifecycle service stubbed out, nothing about the booking changes."""
    booking = make_booking(S.PAID, DAY)
    with mock.patch.object(lifecycle, "mark_delivered"):
        post_mark(owner_client, booking, "delivered", "delivery")
    booking.refresh_from_db()
    assert booking.status == S.PAID


def test_delivering_with_a_balance_owed_asks_for_an_override_note(owner_client, owner):
    booking = make_booking(S.DEPOSIT_PAID, DAY, total="300.00", paid="100.00")
    first = owner_client.post(
        reverse("dashboard:crew_mark", args=[booking.pk]),
        {"action": "delivered", "kind": "delivery", "day": DAY.isoformat()},
        HTTP_HX_REQUEST="true",
    )
    html = first.content.decode()
    assert "still owed" in html
    assert "Deliver anyway" in html
    booking.refresh_from_db()
    assert booking.status == S.DEPOSIT_PAID

    post_mark(
        owner_client, booking, "delivered", "delivery", override_note="Balance collected on site"
    )
    booking.refresh_from_db()
    assert booking.status == S.DELIVERED
    assert booking.delivery_override_by == owner
    assert "Balance collected on site" in booking.delivery_override_note
    assert booking.needs_attention is True


def test_returning_something_not_delivered_is_refused_by_the_service(owner_client):
    booking = make_booking(S.PAID, DAY)
    response = owner_client.post(
        reverse("dashboard:crew_mark", args=[booking.pk]),
        {"action": "returned", "kind": "pickup", "day": DAY.isoformat()},
        HTTP_HX_REQUEST="true",
    )
    assert "Only a delivered booking can be marked returned." in response.content.decode()
    booking.refresh_from_db()
    assert booking.status == S.PAID


def test_marking_needs_post_and_a_known_action(owner_client):
    booking = make_booking(S.PAID, DAY)
    url = reverse("dashboard:crew_mark", args=[booking.pk])
    assert owner_client.get(url).status_code == 405
    assert post_mark(owner_client, booking, "explode", "delivery").status_code == 404


# Crew role -----------------------------------------------------------------


def test_crew_can_mark_delivered_and_returned(crew_client):
    booking = make_booking(S.PAID, DAY)
    post_mark(crew_client, booking, "delivered", "delivery")
    booking.refresh_from_db()
    assert booking.status == S.DELIVERED
    post_mark(crew_client, booking, "returned", "pickup")
    booking.refresh_from_db()
    assert booking.status == S.RETURNED


def test_crew_cannot_see_payments_refunds_or_the_dashboard(crew_client, crew):
    booking = make_booking(S.PAID, DAY)
    assert crew_client.get(reverse("admin:payments_payment_changelist")).status_code == 403
    assert (
        crew_client.get(
            reverse("admin:bookings_booking_ledger", args=[booking.pk, "rental-refund"])
        ).status_code
        == 403
    )
    assert not crew.has_perm("payments.can_issue_refunds")
    assert not crew.has_perm("payments.view_payment")
    assert crew_client.get(reverse("dashboard:calendar")).status_code == 403
    home = crew_client.get(reverse("admin:index"))
    assert home.status_code == 302 and "/crew/" in home["Location"]


def test_crew_cannot_change_a_booking_or_use_its_actions(crew_client, crew):
    booking = make_booking(S.PAID, DAY)
    assert not crew.has_perm("bookings.change_booking")
    page = crew_client.get(reverse("admin:bookings_booking_change", args=[booking.pk]))
    assert page.status_code == 200
    changed = crew_client.post(
        reverse("admin:bookings_booking_change", args=[booking.pk]), {"customer_name": "Changed"}
    )
    assert changed.status_code == 403
    booking.refresh_from_db()
    assert booking.customer_name != "Changed"


def test_crew_booking_detail_shows_no_money(crew_client):
    booking = make_booking(S.PAID, DAY, total="321.00", caution_fee="45.00", caution_paid="45.00")
    html = crew_client.get(
        reverse("admin:bookings_booking_change", args=[booking.pk])
    ).content.decode()
    for text in (
        "321.00",
        "Amount paid",
        "Balance due",
        "Caution held",
        "Invoices and receipts",
        "Unit price",
    ):
        assert text not in html, text


def test_office_cannot_refund_or_open_site_settings(client, office):
    client.force_login(office)
    booking = make_booking(S.PAID, DAY)
    assert (
        client.get(
            reverse("admin:bookings_booking_ledger", args=[booking.pk, "rental-refund"])
        ).status_code
        == 403
    )
    settings_page = client.get(reverse("admin:core_sitesettings_changelist"), follow=True)
    assert settings_page.status_code == 403
    assert (
        client.get(
            reverse("admin:bookings_booking_ledger", args=[booking.pk, "manual-payment"])
        ).status_code
        == 200
    )
    assert client.get(reverse("dashboard:switch")).status_code in (403, 405)


# Visibility switches -------------------------------------------------------


def set_switches(phone, money):
    row = site_settings.get()
    row.crew_sees_customer_phone = phone
    row.crew_sees_money = money
    row.save()


@pytest.fixture
def stop():
    return make_booking(
        S.DEPOSIT_PAID,
        DAY,
        total="400.00",
        paid="150.00",
        caution_fee="50.00",
        caution_paid="50.00",
        phone="469-555-0142",
        customer_name="Johnson",
        pickup_date=date(2027, 6, 13),
    )


def test_defaults_show_the_phone_and_hide_money_from_crew(crew_client, stop):
    html = crew_client.get(day_url()).content.decode()
    assert "469-555-0142" in html
    assert "Collect" not in html
    assert "Balance to collect" not in html
    assert "Caution held" not in html


def test_phone_switch_off_hides_the_phone_from_crew_only(crew_client, owner_client, stop):
    set_switches(phone=False, money=False)
    html = crew_client.get(day_url()).content.decode()
    assert "469-555-0142" not in html
    assert "tel:" not in html
    assert "469-555-0142" in owner_client.get(day_url()).content.decode()


def test_money_switch_on_shows_balance_and_caution_to_crew(crew_client, stop):
    set_switches(phone=True, money=True)
    html = crew_client.get(day_url()).content.decode()
    assert "Collect $250" in html
    assert "Balance to collect" in html
    assert "Caution held" in html


def test_run_sheet_follows_the_same_switches(crew, owner, stop):
    set_switches(phone=False, money=False)
    sheet = runsheet.render_html(DAY, crew)
    assert "469-555-0142" not in sheet
    assert "Collect" not in sheet
    assert "Caution" not in sheet
    full = runsheet.render_html(DAY, owner)
    assert "469-555-0142" in full
    assert "Collect $250" in full
    set_switches(phone=True, money=True)
    assert "469-555-0142" in runsheet.render_html(DAY, crew)
    assert "Collect $250" in runsheet.render_html(DAY, crew)


def test_crew_without_money_are_not_told_amounts_when_delivery_is_blocked(crew_client, stop):
    html = crew_client.post(
        reverse("dashboard:crew_mark", args=[stop.pk]),
        {"action": "delivered", "kind": "delivery", "day": DAY.isoformat()},
        HTTP_HX_REQUEST="true",
    ).content.decode()
    assert "Money or a payment is still outstanding" in html
    assert "$250" not in html
    assert "caution" not in html.lower()


def test_crew_booking_detail_hides_the_phone_when_switched_off(crew_client, stop):
    set_switches(phone=False, money=False)
    html = crew_client.get(
        reverse("admin:bookings_booking_change", args=[stop.pk])
    ).content.decode()
    assert "469-555-0142" not in html


def test_payment_rows_stay_out_of_crew_hands(crew_client, stop):
    assert Payment.objects.filter(booking=stop).exists()
    assert crew_client.get(reverse("admin:payments_payment_changelist")).status_code == 403
