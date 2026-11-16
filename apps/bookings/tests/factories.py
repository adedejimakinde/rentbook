"""factory_boy factories for catalogue, inventory and booking models."""

from datetime import date
from decimal import Decimal

import factory

from apps.bookings.models import Booking, BookingItem, DeliveryZone, PromoCode
from apps.catalogue.models import Category, Item, ItemVariant, Package, PackageItem
from apps.inventory.models import StockAdjustment

EVENT_DATE = date(2027, 6, 12)


class CategoryFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Category

    name = factory.Sequence(lambda n: f"Category {n}")
    slug = factory.Sequence(lambda n: f"category-{n}")


class ItemFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Item

    category = factory.SubFactory(CategoryFactory)
    name = factory.Sequence(lambda n: f"Item {n}")
    slug = factory.Sequence(lambda n: f"item-{n}")
    base_price = Decimal("25.00")


class ItemVariantFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ItemVariant

    item = factory.SubFactory(ItemFactory)
    name = "Default"
    stock_quantity = 10


class StockAdjustmentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = StockAdjustment

    variant = factory.SubFactory(ItemVariantFactory)
    reason = StockAdjustment.Reason.DAMAGED
    quantity = 1


class BookingFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Booking

    customer_name = "Ada Planner"
    email = "ada@example.com"
    phone = "555-0100"
    event_date = EVENT_DATE
    delivery_address = "1 Main St, Dallas, TX"
    status = Booking.Status.PAID


class BookingItemFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = BookingItem

    booking = factory.SubFactory(BookingFactory)
    variant = factory.SubFactory(ItemVariantFactory)
    quantity = 1
    unit_price = Decimal("25.00")


class PackageFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Package

    name = factory.Sequence(lambda n: f"Package {n}")
    slug = factory.Sequence(lambda n: f"package-{n}")
    package_price = Decimal("100.00")


class PackageItemFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PackageItem

    package = factory.SubFactory(PackageFactory)
    variant = factory.SubFactory(ItemVariantFactory)
    quantity = 1


class DeliveryZoneFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = DeliveryZone

    name = factory.Sequence(lambda n: f"Zone {n}")
    zip_codes = ["75001"]
    delivery_fee = Decimal("40.00")
    setup_fee = Decimal("20.00")


class PromoCodeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PromoCode

    code = factory.Sequence(lambda n: f"PROMO{n}")
    percent_off = Decimal("10.00")
