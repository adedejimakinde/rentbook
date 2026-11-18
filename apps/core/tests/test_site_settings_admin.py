"""Site settings in the admin: one row, availability shown, every change logged."""

import re

import pytest
from django.urls import reverse

from apps.core import site_settings
from apps.core.models import SiteSettings, SiteSettingsChange

pytestmark = pytest.mark.django_db
FIELDS = [
    field.name
    for field in SiteSettings._meta.get_fields()
    if getattr(field, "get_internal_type", lambda: "")() == "BooleanField"
]


def change_url():
    return reverse("admin:core_sitesettings_change", args=[site_settings.get().pk])


def form_data(**overrides):
    row = site_settings.get()
    data = {name: "on" for name in FIELDS if getattr(row, name)}
    for name, value in overrides.items():
        if value:
            data[name] = "on"
        else:
            data.pop(name, None)
    return data


def test_the_list_opens_the_single_row(admin_client):
    response = admin_client.get(reverse("admin:core_sitesettings_changelist"))
    assert response.status_code == 302 and response.url == change_url()
    assert SiteSettings.objects.count() == 1


def test_no_second_row_and_no_delete(admin_client):
    site_settings.get()
    assert admin_client.get(reverse("admin:core_sitesettings_add")).status_code == 403
    delete = reverse("admin:core_sitesettings_delete", args=[site_settings.get().pk])
    assert admin_client.get(delete).status_code == 403


def test_page_is_titled_site_settings_and_shows_what_is_not_configured(admin_client, settings):
    settings.TWILIO_ACCOUNT_SID = ""
    settings.STRIPE_SECRET_KEY = "sk_test_x"  # noqa: S105
    html = admin_client.get(change_url()).content.decode()
    assert "Site settings" in html
    assert "Unavailable: not configured (set TWILIO_ACCOUNT_SID" in html
    assert re.search(
        r"<strong>Card or bank account \(ACH\)</strong><span[^>]*>Available</span>", html
    )


def test_a_switched_off_channel_is_shown_as_off(admin_client, settings):
    settings.STRIPE_SECRET_KEY = "sk_test_x"  # noqa: S105
    row = site_settings.get()
    row.card_ach_enabled = False
    row.save()
    html = admin_client.get(change_url()).content.decode()
    assert re.search(r"<strong>Card or bank account \(ACH\)</strong><span[^>]*>Off: ", html)


def test_a_change_is_saved_logged_and_seen_at_once(admin_client, admin_user):
    assert site_settings.online_booking_enabled()
    response = admin_client.post(
        change_url(), form_data(online_booking_enabled=False, zelle_enabled=False)
    )
    assert response.status_code == 302
    assert not site_settings.online_booking_enabled()
    assert not site_settings.get().zelle_enabled
    (change,) = SiteSettingsChange.objects.all()
    assert change.changed_by == admin_user
    assert change.changes == {
        "online_booking_enabled": [True, False],
        "zelle_enabled": [True, False],
    }
    html = admin_client.get(change_url()).content.decode()
    assert "online_booking_enabled: off" in html


def test_saving_without_changes_logs_nothing(admin_client):
    admin_client.post(change_url(), form_data())
    assert not SiteSettingsChange.objects.exists()
