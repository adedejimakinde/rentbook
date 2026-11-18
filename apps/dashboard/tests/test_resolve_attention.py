"""Resolving a booking that needs attention: the service, the dashboard button and the admin."""

import importlib
from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking, BookingLogEntry
from apps.bookings.services import lifecycle
from apps.bookings.tests.factories import StockAdjustmentFactory
from apps.bookings.tests.helpers import add_payment
from apps.dashboard.services import overview
from apps.dashboard.tests.conftest import make_booking
from apps.payments.models import Payment

pytestmark = pytest.mark.django_db
S = Booking.Status
D = Decimal


def flagged(**kwargs):
    return make_booking(
        S.PAID, timezone.localdate() + timedelta(days=20), needs_attention=True, **kwargs
    )


def test_resolving_clears_the_flag_and_logs_note_who_and_when(owner):
    booking = flagged()
    lifecycle.resolve_attention(booking, "  Called the customer, all fine.  ", owner)
    booking.refresh_from_db()
    assert booking.needs_attention is False
    (entry,) = BookingLogEntry.objects.all()
    assert entry.booking == booking
    assert entry.action == BookingLogEntry.RESOLVED_ATTENTION
    assert entry.note == "Called the customer, all fine."
    assert entry.by == owner
    assert entry.created_at


def test_a_note_is_required(owner):
    booking = flagged()
    with pytest.raises(lifecycle.BookingStateError, match="note"):
        lifecycle.resolve_attention(booking, "   ", owner)
    booking.refresh_from_db()
    assert booking.needs_attention is True
    assert not BookingLogEntry.objects.exists()


def test_a_booking_that_is_not_flagged_cannot_be_resolved(owner):
    booking = make_booking(S.PAID)
    with pytest.raises(lifecycle.BookingStateError, match="not flagged"):
        lifecycle.resolve_attention(booking, "note", owner)


def test_refused_while_a_payment_mismatch_is_pending(owner):
    booking = flagged()
    payment = add_payment(booking, "5.00", status=Payment.Status.PENDING)
    payment.mismatch_flagged_at = timezone.now()
    payment.save()
    with pytest.raises(lifecycle.BookingStateError, match="different amount"):
        lifecycle.resolve_attention(booking, "note", owner)
    Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.FAILED)
    lifecycle.resolve_attention(booking, "Refunded the difference", owner)
    booking.refresh_from_db()
    assert booking.needs_attention is False


def test_refused_while_a_refund_is_pending_but_not_once_it_has_failed_or_settled(owner):
    booking = flagged()
    refund = add_payment(
        booking, "20.00", kind=Payment.Kind.RENTAL_REFUND, status=Payment.Status.PENDING
    )
    with pytest.raises(lifecycle.BookingStateError, match="refund is still pending"):
        lifecycle.resolve_attention(booking, "note", owner)
    Payment.objects.filter(pk=refund.pk).update(status=Payment.Status.SUCCEEDED)
    lifecycle.resolve_attention(booking, "Refund went through", owner)
    booking.refresh_from_db()
    assert booking.needs_attention is False


def test_refused_while_stock_is_still_short(owner):
    booking = flagged()
    variant = booking.items.get().variant
    variant.stock_quantity = 1
    variant.save()
    adjustment = StockAdjustmentFactory(variant=variant, quantity=1)
    with pytest.raises(lifecycle.BookingStateError, match="stock is still short"):
        lifecycle.resolve_attention(booking, "note", owner)
    adjustment.resolved_at = timezone.now()
    adjustment.save()
    lifecycle.resolve_attention(booking, "Unit repaired", owner)
    booking.refresh_from_db()
    assert booking.needs_attention is False


def test_a_second_resolve_is_refused(owner):
    booking = flagged()
    lifecycle.resolve_attention(booking, "done", owner)
    with pytest.raises(lifecycle.BookingStateError):
        lifecycle.resolve_attention(booking, "again", owner)
    assert BookingLogEntry.objects.count() == 1


# Dashboard ------------------------------------------------------------------


def resolve_url(booking):
    return reverse("dashboard:resolve_attention", args=[booking.pk])


def test_each_row_has_a_resolve_button_for_owner(owner_client):
    booking = flagged()
    html = owner_client.get(reverse("admin:index")).content.decode()
    assert "Resolve</summary>" in html
    assert resolve_url(booking) in html


def test_resolving_from_the_dashboard_updates_the_card(owner_client, owner):
    booking = flagged(customer_name="Martinez")
    other = flagged(customer_name="Okafor")
    response = owner_client.post(resolve_url(booking), {"note": "Sorted"}, HTTP_HX_REQUEST="true")
    html = response.content.decode()
    assert response.status_code == 200
    assert "<html" not in html
    assert "Martinez" not in html
    assert "Okafor" in html
    other.refresh_from_db()
    assert other.needs_attention is True
    assert BookingLogEntry.objects.get().by == owner


def test_a_refused_resolve_shows_why_on_the_row(owner_client):
    booking = flagged()
    add_payment(booking, "20.00", kind=Payment.Kind.CAUTION_REFUND, status=Payment.Status.PENDING)
    html = owner_client.post(
        resolve_url(booking), {"note": "Sorted"}, HTTP_HX_REQUEST="true"
    ).content.decode()
    assert "Cannot resolve yet: a refund is still pending." in html
    booking.refresh_from_db()
    assert booking.needs_attention is True


def test_resolving_without_javascript_redirects_with_a_message(owner_client):
    booking = flagged()
    response = owner_client.post(resolve_url(booking), {"note": "Sorted"}, follow=True)
    assert response.status_code == 200
    assert f"Booking #{booking.pk} resolved." in response.content.decode()


def test_office_can_resolve_and_crew_cannot(client, office, crew_client):
    from django.test import Client

    booking = flagged()
    assert crew_client.post(resolve_url(booking), {"note": "x"}).status_code == 403
    booking.refresh_from_db()
    assert booking.needs_attention is True
    office_client = Client()
    office_client.force_login(office)
    assert office_client.post(resolve_url(booking), {"note": "Sorted"}).status_code == 302
    booking.refresh_from_db()
    assert booking.needs_attention is False


def test_the_card_count_drops_after_resolving(owner):
    booking = flagged()
    flagged()
    assert len(overview.attention_rows()) == 2
    lifecycle.resolve_attention(booking, "ok", owner)
    assert len(overview.attention_rows()) == 1


# Booking detail ---------------------------------------------------------------


def test_the_booking_page_offers_resolve_only_while_flagged(owner_client):
    booking = flagged()
    page = reverse("admin:bookings_booking_change", args=[booking.pk])
    assert "Resolve attention" in owner_client.get(page).content.decode()
    Booking.objects.filter(pk=booking.pk).update(needs_attention=False)
    assert "Resolve attention" not in owner_client.get(page).content.decode()


def test_the_admin_form_resolves_through_the_service(owner_client, owner):
    booking = flagged()
    url = reverse("admin:bookings_booking_ledger", args=[booking.pk, "resolve-attention"])
    assert owner_client.get(url).status_code == 200
    response = owner_client.post(url, {"note": "Customer confirmed by phone"})
    assert response.status_code == 302
    booking.refresh_from_db()
    assert booking.needs_attention is False
    entry = BookingLogEntry.objects.get()
    assert (entry.note, entry.by) == ("Customer confirmed by phone", owner)


def test_the_admin_form_shows_a_refusal_and_keeps_the_flag(owner_client):
    booking = flagged()
    add_payment(booking, "20.00", kind=Payment.Kind.RENTAL_REFUND, status=Payment.Status.PENDING)
    url = reverse("admin:bookings_booking_ledger", args=[booking.pk, "resolve-attention"])
    html = owner_client.post(url, {"note": "x"}).content.decode()
    assert "refund is still pending" in html
    booking.refresh_from_db()
    assert booking.needs_attention is True


def test_crew_cannot_use_the_admin_resolve_form(crew_client):
    booking = flagged()
    url = reverse("admin:bookings_booking_ledger", args=[booking.pk, "resolve-attention"])
    assert crew_client.get(url).status_code == 403


def test_the_log_shows_on_the_booking_page_for_office_but_not_crew(
    client, office, crew_client, owner
):
    booking = flagged()
    lifecycle.resolve_attention(booking, "Called and confirmed", owner)
    page = reverse("admin:bookings_booking_change", args=[booking.pk])
    client.force_login(office)
    assert "Called and confirmed" in client.get(page).content.decode()
    assert "Called and confirmed" not in crew_client.get(page).content.decode()


def test_office_group_has_the_new_permissions_from_the_migration(office):
    assert office.has_perm("bookings.resolve_attention")
    assert office.has_perm("bookings.view_bookinglogentry")


def test_the_city_filler_reads_the_last_address_line():
    module = importlib.import_module("apps.bookings.migrations.0014_fill_booking_city")
    fill = module.city_from_address
    assert fill("12 Elm St\nGarland, TX 75040") == "Garland"
    assert fill("12 Elm St\nApt 4\nFort Worth, TX 76102") == "Fort Worth"
    assert fill("12 Elm St, Plano") == ""
    assert fill("") == ""
    assert fill("Just one line") == ""


def test_the_data_migration_fills_city_for_existing_bookings_and_keeps_set_ones():
    from django.apps import apps

    module = importlib.import_module("apps.bookings.migrations.0014_fill_booking_city")
    old = make_booking(S.PAID, delivery_address="1 Elm St\nGarland, TX 75040", city="")
    odd = make_booking(S.PAID, delivery_address="no city here", city="")
    kept = make_booking(S.PAID, delivery_address="2 Elm St\nPlano, TX 75024", city="Frisco")
    module.fill_city(apps, None)
    for booking in (old, odd, kept):
        booking.refresh_from_db()
    assert (old.city, odd.city, kept.city) == ("Garland", "", "Frisco")
