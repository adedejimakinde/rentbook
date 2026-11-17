"""Tests for the home page and health endpoint."""

import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_home_page_renders_brand_and_contact(client):
    response = client.get(reverse("core:home"))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Atglace Events &amp; Rentals LLC" in html
    assert "Your one-stop shop for event rentals" in html
    assert "https://wa.me/14693474687" in html
    assert "tel:+14693474687" in html


@pytest.mark.django_db
def test_health_endpoint_reports_ok(client):
    response = client.get("/health/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "up"}


@pytest.mark.django_db
def test_manifest_lists_icons(client):
    response = client.get(reverse("core:manifest"))
    assert response["Content-Type"] == "application/manifest+json"
    assert {i["sizes"] for i in response.json()["icons"]} == {"192x192", "512x512"}


@pytest.mark.django_db
def test_base_has_logo_icons_and_default_og_image(client):
    html = client.get(reverse("core:home")).content.decode()
    assert 'alt="Atglace Events &amp; Rentals LLC"' in html
    assert "atglace-logo-header-320.webp" in html
    assert "atglace-logo-header-white.webp" in html
    assert "apple-touch-icon.png" in html
    assert "og-icon-512.png" in html
    assert "Logo placeholder" not in html
