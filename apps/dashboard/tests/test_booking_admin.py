"""Booking list: filters, search, money columns and phone cards."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.tests.helpers import add_payment
from apps.dashboard.services.money import with_money
from apps.dashboard.tests.conftest import make_booking
from apps.payments.models import Payment

pytestmark = pytest.mark.django_db
S = Booking.Status
D = Decimal
LIST = "admin:bookings_booking_changelist"


def ids(response):
    return {booking.pk for booking in response.context["cl"].result_list}


def search(client, **params):
    return ids(client.get(reverse(LIST), params))


def test_money_annotations_match_the_booking_properties():
    booking = make_booking(
        S.DEPOSIT_PAID,
        total="400.00",
        paid="150.00",
        caution_fee="50.00",
        caution_paid="30.00",
        delivery_fee=D("20.00"),
        discount=D("10.00"),
        tax=D("5.00"),
    )
    add_payment(booking, "40.00", kind=Payment.Kind.BALANCE, refunded_amount="15.00")
    add_payment(booking, "999.00", status=Payment.Status.FAILED)
    row = with_money(Booking.objects.filter(pk=booking.pk)).get()
    assert row.m_total == booking.total
    assert row.m_paid == booking.amount_paid
    assert row.m_balance == booking.balance_due
    assert row.m_caution_held == booking.caution_held
    assert row.m_caution_due == booking.caution_due


def test_columns_show_event_date_total_paid_and_balance(owner_client):
    make_booking(S.DEPOSIT_PAID, total="400.00", paid="150.00")
    html = owner_client.get(reverse(LIST)).content.decode()
    for text in ("$400.00", "$150.00", "$250.00", "Event date", "Total", "Paid", "Balance"):
        assert text in html, text


def test_event_date_range_filter(owner_client):
    early = make_booking(S.PAID, date(2027, 6, 1))
    middle = make_booking(S.PAID, date(2027, 6, 15))
    late = make_booking(S.PAID, date(2027, 7, 1))
    assert search(owner_client, event_date_from="2027-06-10", event_date_to="2027-06-20") == {
        middle.pk
    }
    assert search(owner_client, event_date_from="2027-06-10") == {middle.pk, late.pk}
    assert search(owner_client, event_date_to="2027-06-10") == {early.pk}


def test_status_filter_takes_one_or_several(owner_client):
    paid = make_booking(S.PAID)
    deposit = make_booking(S.DEPOSIT_PAID)
    returned = make_booking(S.RETURNED)
    assert search(owner_client, status="paid") == {paid.pk}
    assert search(owner_client, status="paid,deposit_paid") == {paid.pk, deposit.pk}
    assert returned.pk in search(owner_client)


def test_payment_state_filter(owner_client):
    unpaid = make_booking(S.QUOTED, total="100.00", paid="0.00")
    part = make_booking(S.DEPOSIT_PAID, total="100.00", paid="30.00")
    full = make_booking(S.PAID, total="100.00", paid="100.00")
    assert search(owner_client, payment_state="unpaid") == {unpaid.pk}
    assert search(owner_client, payment_state="part_paid") == {part.pk}
    assert search(owner_client, payment_state="balance_due") == {unpaid.pk, part.pk}
    assert search(owner_client, payment_state="paid") == {full.pk}


def test_needs_attention_filter(owner_client):
    flagged = make_booking(S.PAID, needs_attention=True)
    make_booking(S.PAID)
    assert search(owner_client, needs_attention__exact="1") == {flagged.pk}


def test_payment_method_filter(owner_client):
    zelle = make_booking(S.PAID)
    card = make_booking(S.PAID)
    chose_cashapp = make_booking(S.QUOTED, payment_choice="cashapp")
    Payment.objects.filter(booking=card).update(method=Payment.Method.CARD)
    assert search(owner_client, payment_method="zelle") == {zelle.pk}
    assert search(owner_client, payment_method="card") == {card.pk}
    assert search(owner_client, payment_method="cashapp") == {chose_cashapp.pk}


def test_caution_filter_and_still_no_duplicates(owner_client):
    held = make_booking(S.PAID, caution_fee="50.00", caution_paid="50.00")
    make_booking(S.PAID)
    add_payment(held, "10.00", kind=Payment.Kind.BALANCE)
    assert search(owner_client, caution="held") == {held.pk}
    assert (
        len(owner_client.get(reverse(LIST), {"payment_method": "zelle"}).context["cl"].result_list)
        == 2
    )


def test_search_by_name_email_phone_and_number(owner_client):
    ada = make_booking(
        S.PAID, customer_name="Ada Obi", email="ada@example.com", phone="(469) 555-0142"
    )
    other = make_booking(
        S.PAID, customer_name="Grace Hall", email="grace@church.org", phone="214-555-0199"
    )
    assert search(owner_client, q="obi") == {ada.pk}
    assert search(owner_client, q="church.org") == {other.pk}
    assert search(owner_client, q="469-555-0142") == {ada.pk}
    assert search(owner_client, q="4695550142") == {ada.pk}
    assert search(owner_client, q="214 555") == {other.pk}
    assert ada.pk in search(owner_client, q=str(ada.pk))
    assert search(owner_client, q=f"#{ada.pk}") == {ada.pk}
    assert search(owner_client, q=f"#{other.pk}") == {other.pk}
    assert search(owner_client, q="nobody") == set()


def test_phone_cards_are_in_the_page_alongside_the_table(owner_client):
    booking = make_booking(S.DEPOSIT_PAID, total="400.00", paid="150.00", customer_name="Ada Obi")
    html = owner_client.get(reverse(LIST)).content.decode()
    assert 'class="bk-cards"' in html
    assert f"#{booking.pk} Ada Obi" in html
    assert "bk-status-deposit_paid" in html
    assert "Deposit paid" in html
    assert 'id="result_list"' in html


def test_crew_booking_list_has_no_money_columns_or_cards_money(crew_client):
    make_booking(S.PAID, total="321.00", customer_name="Ada Obi")
    html = crew_client.get(reverse(LIST)).content.decode()
    assert "Ada Obi" in html
    assert "321.00" not in html
    assert "bk-balance" not in html


def test_crew_has_no_actions_on_the_booking_list(crew_client):
    make_booking(S.PAID)
    response = crew_client.get(reverse(LIST))
    assert b"Record manual payment" not in response.content


def test_old_bookings_are_still_found_with_the_default_ordering(owner_client):
    old = make_booking(S.RETURNED, timezone.localdate() - timedelta(days=400))
    assert old.pk in search(owner_client)
