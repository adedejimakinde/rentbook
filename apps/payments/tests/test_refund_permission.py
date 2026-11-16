"""Refunds need the separate "can issue refunds" permission."""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse

from apps.bookings.models import Booking
from apps.payments.models import Payment
from apps.payments.tests.helpers import held_booking, pay

pytestmark = pytest.mark.django_db

D = Decimal
REFUND_ACTIONS = ["caution-refund", "rental-refund"]
OTHER_ACTIONS = ["manual-payment", "damage-charge", "cancel"]


def staff_with(*codenames):
    user = get_user_model().objects.create_user("clerk", is_staff=True)
    perms = Permission.objects.filter(
        content_type__app_label__in=["bookings", "payments"], codename__in=codenames
    )
    user.user_permissions.set(perms)
    return user


BOOKING_PERMS = ("view_booking", "change_booking")


@pytest.fixture
def booking(client):
    booking = held_booking(total="200.00", caution_fee="75.00")
    pay(client, booking, Payment.Kind.FULL)
    return booking


def ledger_url(booking, action):
    return reverse("admin:bookings_booking_ledger", args=[booking.pk, action])


def test_permission_exists():
    permission = Permission.objects.get(codename="can_issue_refunds")
    assert permission.content_type.app_label == "payments"
    assert permission.name == "Can issue refunds"


@pytest.mark.parametrize("action", REFUND_ACTIONS)
def test_refund_forms_are_refused_without_the_permission(client, booking, action):
    client.force_login(staff_with(*BOOKING_PERMS))

    assert client.get(ledger_url(booking, action)).status_code == 403
    assert client.post(ledger_url(booking, action), {"amount": "10.00"}).status_code == 403


@pytest.mark.parametrize("action", OTHER_ACTIONS)
def test_other_ledger_forms_do_not_need_it(client, booking, action):
    client.force_login(staff_with(*BOOKING_PERMS))
    assert client.get(ledger_url(booking, action)).status_code == 200


@pytest.mark.parametrize("action", REFUND_ACTIONS)
def test_refund_forms_open_with_the_permission(client, booking, action):
    client.force_login(staff_with(*BOOKING_PERMS, "can_issue_refunds"))
    assert client.get(ledger_url(booking, action)).status_code == 200


def test_refund_actions_are_hidden_without_the_permission(client, booking):
    client.force_login(staff_with(*BOOKING_PERMS))
    actions = client.get(reverse("admin:bookings_booking_changelist")).context["action_form"]
    names = {value for value, _ in actions.fields["action"].choices}
    assert "record_manual_payment" in names
    assert "refund_rental_payment" not in names
    assert "record_caution_refund" not in names


def test_refund_action_cannot_be_posted_without_the_permission(client, booking):
    client.force_login(staff_with(*BOOKING_PERMS))
    response = client.post(
        reverse("admin:bookings_booking_changelist"),
        {"action": "refund_rental_payment", "_selected_action": [booking.pk]},
    )
    assert response.status_code != 302 or "ledger" not in response.get("Location", "")
    assert Booking.objects.get(pk=booking.pk).status == Booking.Status.PAID


def test_refund_actions_are_offered_with_the_permission(client, booking):
    client.force_login(staff_with(*BOOKING_PERMS, "can_issue_refunds"))
    actions = client.get(reverse("admin:bookings_booking_changelist")).context["action_form"]
    names = {value for value, _ in actions.fields["action"].choices}
    assert {"refund_rental_payment", "record_caution_refund"} <= names
