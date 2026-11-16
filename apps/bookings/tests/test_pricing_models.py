"""Validation and admin for delivery zones, promo codes and packages."""

from decimal import Decimal as D

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.bookings.models import DeliveryZone, PromoCode
from apps.bookings.tests.factories import (
    DeliveryZoneFactory,
    PackageItemFactory,
    PromoCodeFactory,
)

pytestmark = pytest.mark.django_db


def test_zip_cannot_be_in_two_active_zones():
    DeliveryZoneFactory(name="Dallas", zip_codes=["75001", "75002"])
    clash = DeliveryZone(name="Plano", zip_codes=["75002", "75023"])

    with pytest.raises(ValidationError, match="75002 already in Dallas"):
        clash.full_clean()


def test_zip_can_be_reused_when_either_zone_is_inactive():
    DeliveryZoneFactory(name="Old Dallas", zip_codes=["75001"], is_active=False)
    DeliveryZone(name="Dallas", zip_codes=["75001"]).full_clean()

    DeliveryZoneFactory(name="Plano", zip_codes=["75023"])
    DeliveryZone(name="Old Plano", zip_codes=["75023"], is_active=False).full_clean()


def test_zone_can_be_saved_again_and_zips_are_tidied():
    zone = DeliveryZoneFactory(zip_codes=["75002"])
    zone.zip_codes = ["75002", "75001", "75002"]

    zone.full_clean()

    assert zone.zip_codes == ["75001", "75002"]


def test_zone_rejects_bad_zip_codes():
    with pytest.raises(ValidationError, match="Not a 5-digit ZIP code: 7500"):
        DeliveryZone(name="Typo", zip_codes=["7500"]).full_clean()


@pytest.mark.parametrize(("percent_off", "amount_off"), [(None, None), (D("10"), D("5.00"))])
def test_promo_needs_exactly_one_kind_of_discount(percent_off, amount_off):
    promo = PromoCode(code="X", percent_off=percent_off, amount_off=amount_off)

    with pytest.raises(ValidationError, match="either a percent off or an amount off"):
        promo.full_clean()
    with pytest.raises(IntegrityError), transaction.atomic():
        promo.save()


def test_promo_code_is_stored_upper_case_and_unique_ignoring_case():
    promo = PromoCode(code=" spring10 ", percent_off=D("10"))
    promo.full_clean()
    promo.save()

    assert promo.code == "SPRING10"
    with pytest.raises(IntegrityError), transaction.atomic():
        PromoCodeFactory(code="Spring10")


def test_promo_percent_cannot_exceed_100():
    with pytest.raises(ValidationError):
        PromoCode(code="TOOMUCH", percent_off=D("101")).full_clean()


def test_pricing_admin_pages_render(admin_client):
    zone = DeliveryZoneFactory()
    promo = PromoCodeFactory()
    package = PackageItemFactory().package
    for name, obj in [
        ("bookings_deliveryzone", zone),
        ("bookings_promocode", promo),
        ("catalogue_package", package),
    ]:
        assert admin_client.get(reverse(f"admin:{name}_changelist")).status_code == 200
        assert admin_client.get(reverse(f"admin:{name}_add")).status_code == 200
        assert admin_client.get(reverse(f"admin:{name}_change", args=[obj.pk])).status_code == 200


def test_admin_saves_a_zone_and_rejects_an_overlapping_one(admin_client):
    url = reverse("admin:bookings_deliveryzone_add")
    form = {"delivery_fee": "40.00", "setup_fee": "20.00", "is_active": "on"}

    saved = admin_client.post(url, {**form, "name": "Dallas", "zip_codes": "75001, 75002"})
    clash = admin_client.post(url, {**form, "name": "Plano", "zip_codes": "75002"})

    assert saved.status_code == 302
    assert DeliveryZone.objects.get().zip_codes == ["75001", "75002"]
    assert clash.status_code == 200
    assert DeliveryZone.objects.count() == 1


def test_admin_cannot_edit_promo_uses_count(admin_client):
    promo = PromoCodeFactory(code="KEEP", uses_count=3)
    response = admin_client.post(
        reverse("admin:bookings_promocode_change", args=[promo.pk]),
        {
            "code": "keep",
            "percent_off": "15",
            "min_subtotal": "0",
            "uses_count": "0",
            "is_active": "on",
        },
    )

    promo.refresh_from_db()
    assert response.status_code == 302
    assert (promo.code, promo.percent_off, promo.uses_count) == ("KEEP", D("15.00"), 3)
