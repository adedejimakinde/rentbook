"""The Owner, Office and Crew groups made by the data migration."""

import pytest
from django.contrib.auth.models import Group, Permission

from apps.dashboard import roles

pytestmark = pytest.mark.django_db


def codenames(group_name):
    group = Group.objects.get(name=group_name)
    return {f"{p.content_type.app_label}.{p.codename}" for p in group.permissions.all()}


def test_three_groups_exist():
    assert set(Group.objects.values_list("name", flat=True)) >= {"Owner", "Office", "Crew"}


def test_owner_has_every_permission():
    assert Group.objects.get(name="Owner").permissions.count() == Permission.objects.count()


def test_office_runs_bookings_but_cannot_refund_or_touch_settings():
    perms = codenames("Office")
    assert {"bookings.change_booking", "bookings.view_dashboard", "payments.view_payment"} <= perms
    assert "payments.can_issue_refunds" not in perms
    assert "bookings.can_set_tax_exemption" not in perms
    assert not {p for p in perms if p.startswith("core.")}
    assert "payments.change_payment" not in perms
    assert "payments.add_payment" not in perms


def test_crew_has_the_schedule_and_read_only_bookings_only():
    perms = codenames("Crew")
    assert perms == {
        "bookings.view_booking",
        "bookings.view_bookingitem",
        "bookings.view_bookingpackage",
        "bookings.view_crew_schedule",
    }


def test_the_role_lists_only_name_real_permissions():
    for app_label, codename in (*roles.OFFICE_PERMISSIONS, *roles.CREW_PERMISSIONS):
        assert Permission.objects.filter(
            content_type__app_label=app_label, codename=codename
        ).exists(), (app_label, codename)
