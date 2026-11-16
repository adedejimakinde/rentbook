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
