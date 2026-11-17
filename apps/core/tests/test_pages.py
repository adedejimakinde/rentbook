"""Quick check, contact form and link-in-bio behaviour."""

import pytest
from django.core import mail

from apps.bookings.tests.factories import DeliveryZoneFactory

pytestmark = pytest.mark.django_db

HTMX = {"HTTP_HX_REQUEST": "true"}


def test_quick_check_uses_delivery_zones_and_remembers_the_choice(client, event_date):
    DeliveryZoneFactory(name="Dallas core", zip_codes=["75201"])

    response = client.post("/check/", {"date": f"{event_date:%Y-%m-%d}", "zip": "75201"}, **HTMX)
    html = response.content.decode()

    assert "We deliver to 75201 (Dallas core)" in html
    assert "<html" not in html
    assert "Delivery from $40.00" in html
    assert client.get("/").content.decode().count('value="75201"') == 1
    assert f'value="{event_date:%Y-%m-%d}"' in client.get("/").content.decode()


def test_quick_check_for_a_zip_we_do_not_serve(client, event_date):
    html = client.post(
        "/check/", {"date": f"{event_date:%Y-%m-%d}", "zip": "10001"}, **HTMX
    ).content.decode()

    assert "do not have 10001 on our delivery list" in html
    assert "We deliver to" not in html


def test_quick_check_rejects_bad_input(client):
    html = client.post("/check/", {"date": "", "zip": "12"}, **HTMX).content.decode()

    assert "Choose your event date" in html
    assert "5-digit ZIP" in html


def test_quick_check_without_htmx_renders_a_full_page(client, event_date):
    html = client.post(
        "/check/", {"date": f"{event_date:%Y-%m-%d}", "zip": "75201"}
    ).content.decode()

    assert "<html" in html and "Check availability" in html


def test_quick_check_requires_post(client):
    assert client.get("/check/").status_code == 405


def test_contact_form_emails_the_owner(client, settings):
    response = client.post(
        "/contact/",
        {
            "name": "Ada Planner",
            "email": "ada@example.com",
            "phone": "555-0100",
            "event_date": "2027-06-12",
            "message": "Do you have 80 gold chairs?",
        },
    )

    assert response.status_code == 302
    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.to == [settings.BUSINESS_EMAIL]
    assert sent.reply_to == ["ada@example.com"]
    assert "80 gold chairs" in sent.body and "2027-06-12" in sent.body
    assert "Thanks, we have your message" in client.get("/contact/").content.decode()


def test_contact_form_validates_and_labels_every_field(client):
    response = client.post("/contact/", {"name": "", "email": "nope", "message": ""})
    html = response.content.decode()

    assert response.status_code == 200
    assert mail.outbox == []
    assert html.count('role="alert"') >= 3
    for field in ("name", "email", "phone", "event_date", "message"):
        assert f'for="id_{field}"' in html


def test_contact_honeypot_sends_nothing(client):
    client.post(
        "/contact/",
        {
            "name": "Bot",
            "email": "bot@example.com",
            "message": "buy now",
            "website": "spam.example",
        },
    )

    assert mail.outbox == []


def test_contact_is_rate_limited_per_session(client):
    data = {"name": "Ada", "email": "ada@example.com", "message": "Hello"}

    for _ in range(7):
        client.post("/contact/", data)

    assert len(mail.outbox) == 5


def test_links_page_has_big_buttons_and_no_site_chrome(client):
    html = client.get("/links/").content.decode()

    for label in ("Book now", "Browse rentals", "Chat on WhatsApp", "Call "):
        assert label in html
    assert "wa.me/14693474687" in html and "tel:+14693474687" in html
    assert "<header" not in html and "<footer" not in html


def test_faq_uses_configured_notice_and_deposit(client, settings):
    settings.MIN_NOTICE_HOURS = 36
    settings.DEPOSIT_PERCENT = 30

    html = client.get("/faq/").content.decode()

    assert "36 hours" in html and "30% deposit" in html
