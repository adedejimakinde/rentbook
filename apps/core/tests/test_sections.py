"""Optional sections in the nav and footer, and the rental terms page."""

import pytest
from django.urls import reverse

from apps.bookings.tests.factories import ItemFactory, PackageFactory, PackageItemFactory
from apps.catalogue.models import ItemImage
from apps.core.services import section_flags

pytestmark = pytest.mark.django_db


def nav_html(client):
    html = client.get(reverse("core:about")).content.decode()
    return html[html.index("<header") : html.index("<main")], html[html.index("<footer") :]


def test_nav_and_footer_hide_packages_and_gallery_with_no_content(client):
    PackageFactory(name="Empty package")  # active but with no items
    for part in nav_html(client):
        assert 'href="/packages/"' not in part
        assert 'href="/gallery/"' not in part
        assert 'href="/rentals/"' in part


def test_inactive_packages_and_unprocessed_photos_do_not_count(client):
    package = PackageFactory(is_active=False)
    PackageItemFactory(package=package)
    ItemImage.objects.bulk_create([ItemImage(item=ItemFactory(), image="items/x.jpg")])

    assert section_flags() == {"show_packages": False, "show_gallery": False}


def test_sections_appear_once_they_have_content(client, django_capture_on_commit_callbacks):
    nav_html(client)  # caches the empty state
    with django_capture_on_commit_callbacks(execute=True):
        PackageItemFactory(package=PackageFactory())
        photo = ItemImage.objects.create(item=ItemFactory(), image="items/x.jpg")
        ItemImage.objects.filter(pk=photo.pk).update(renditions_ready=True)
        photo.save(update_fields=["alt_text"])

    for part in nav_html(client):
        assert 'href="/packages/"' in part
        assert 'href="/gallery/"' in part


def test_section_flags_are_cached(client, django_assert_num_queries):
    section_flags()

    with django_assert_num_queries(0):
        section_flags()


def test_rental_terms_page_has_placeholder_sections_and_version(client, settings):
    settings.TERMS_VERSION = "2026-11-v2"

    html = client.get(reverse("core:terms")).content.decode()

    for heading in ("Cancellation", "Weather", "Damage", "Setup requirements"):
        assert heading in html
    assert "Version 2026-11-v2" in html
    assert 'href="/rental-terms/"' in html[html.index("<footer") :]


def test_robots_keeps_private_booking_pages_out(client):
    text = client.get("/robots.txt").content.decode()

    for path in ("/cart/", "/book/", "/booking/", "/payments/"):
        assert f"Disallow: {path}" in text
