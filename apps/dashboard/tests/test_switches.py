"""Quick switches: the five owner settings, logged with who and when, owner only."""

import pytest
from django.urls import reverse

from apps.core import site_settings
from apps.core.models import SiteSettings, SiteSettingsChange

pytestmark = pytest.mark.django_db
URL = "dashboard:switch"


def post(client, name, on, **extra):
    data = {"switch": name}
    if on:
        data["on"] = "1"
    return client.post(reverse(URL), data, **extra)


def test_new_switches_have_the_agreed_defaults():
    row = site_settings.get()
    assert row.crew_sees_customer_phone is True
    assert row.crew_sees_money is False
    assert row.wind_alerts_enabled is True
    assert row.online_booking_enabled is True


def test_the_home_page_shows_all_five_switches_in_order(owner_client):
    html = owner_client.get(reverse("admin:index")).content.decode()
    labels = [
        "Online booking",
        "Crew sees customer phone",
        "Crew sees money",
        "Text reminders",
        "Wind alerts",
    ]
    positions = [html.index(f">{label}</span>") for label in labels]
    assert positions == sorted(positions)
    assert html.count('role="switch"') == 5


def test_turning_a_switch_off_saves_logs_and_takes_effect(owner_client, owner):
    response = post(owner_client, "online_booking_enabled", False, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    assert not site_settings.online_booking_enabled()
    (change,) = SiteSettingsChange.objects.all()
    assert change.changed_by == owner
    assert change.changes == {"online_booking_enabled": [True, False]}
    assert change.created_at


def test_each_switch_can_be_turned_both_ways(owner_client):
    for name in site_settings.QUICK_SWITCHES:
        current = getattr(SiteSettings.objects.get_or_create(pk=1)[0], name)
        post(owner_client, name, not current)
        assert getattr(SiteSettings.objects.get(pk=1), name) is (not current)


def test_setting_a_switch_to_its_current_value_logs_nothing(owner_client):
    post(owner_client, "crew_sees_customer_phone", True)
    assert not SiteSettingsChange.objects.exists()


def test_only_quick_switches_can_be_set_this_way(owner_client):
    assert post(owner_client, "zelle_enabled", False).status_code == 404
    assert site_settings.get().zelle_enabled is True


def test_office_sees_no_switches_and_cannot_set_them(client, office):
    client.force_login(office)
    assert "Quick switches" not in client.get(reverse("admin:index")).content.decode()
    assert post(client, "crew_sees_money", True).status_code == 403
    assert site_settings.get().crew_sees_money is False


def test_crew_cannot_set_switches(crew_client):
    assert post(crew_client, "crew_sees_money", True).status_code == 403


def test_the_switch_change_shows_in_the_site_settings_history(owner_client):
    post(owner_client, "crew_sees_money", True)
    row = site_settings.get()
    page = owner_client.get(reverse("admin:core_sitesettings_change", args=[row.pk]))
    html = page.content.decode()
    assert "crew_sees_money: on" in html
    assert "Crew sees money" in html


def test_text_reminders_say_when_texting_is_not_set_up(owner_client, settings):
    settings.TWILIO_ACCOUNT_SID = ""
    html = owner_client.get(reverse("admin:index")).content.decode()
    assert "Texting is not set up yet" in html
