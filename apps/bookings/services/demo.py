"""Demo data for showing the site in development. Never called outside seed_demo.

Prices, stock, zones and packages are set to fixed values on every run. Demo bookings are
keyed by their ``@demo.example`` email, so a second run adds nothing and leaves them alone.
Rows are written directly, so no invoice, email or text is queued for the fake customers.
"""

from dataclasses import dataclass, field
from datetime import time, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import transaction
from django.utils import timezone

from apps.bookings.models import (
    Booking,
    BookingItem,
    BookingPackage,
    DeliveryZone,
    EventType,
    Surface,
)
from apps.bookings.services.quote import sales_tax
from apps.catalogue.models import Item, ItemVariant, Package, PackageItem
from apps.catalogue.services.seed import seed_catalogue
from apps.dashboard import roles
from apps.payments.models import Payment

DEMO_EMAIL_DOMAIN = "demo.example"
DEMO_PASSWORD = "demo1234"  # noqa: S105
OWNER_USERNAME = "owner"
CREW_USERNAME = "crew"

# item name -> (price per unit, caution fee per unit, {variant name: stock})
PRICING = {
    "Bounce house": ("150", "50", {"Default": 3}),
    "Combo bounce house": ("200", "75", {"Default": 2}),
    "Water slide": ("225", "75", {"Default": 2}),
    "Obstacle course": ("250", "100", {"Default": 1}),
    "Canopy": ("60", "0", {"Default": 8}),
    "Round table": ("10", "0", {"Default": 30}),
    "Rectangle table": ("9", "0", {"Default": 40}),
    "Kids table": ("8", "0", {"Default": 12}),
    "Kids chair": ("2", "0", {"Default": 60}),
    "Garden chair": ("2.50", "0", {"Black": 100, "White": 100}),
    "Chiavari chair": ("5", "0", {"Black": 60, "Silver": 60, "Gold": 120}),
    "Popcorn machine": ("55", "25", {"Default": 3}),
    "Cotton candy machine": ("65", "25", {"Default": 3}),
    "Margarita machine": ("120", "50", {"Default": 1}),
    "Slushy machine": ("85", "35", {"Default": 2}),
    "Photo backdrop": ("85", "0", {"Default": 4}),
    "Table covers and linen": ("8", "0", {"Default": 60}),
    "Golf simulator": ("450", "150", {"Default": 1}),
    "Generator": ("90", "50", {"Default": 2}),
}

ZONES = [
    (
        "Dallas core",
        ["75201", "75202", "75204", "75205", "75206", "75207", "75208", "75214", "75219", "75225"],
        "35.00",
        "40.00",
    ),
    (
        "North DFW (Plano, Frisco, McKinney)",
        ["75023", "75024", "75074", "75075", "75034", "75035", "75069", "75070", "75013", "75080"],
        "65.00",
        "50.00",
    ),
    (
        "West DFW (Fort Worth, Arlington)",
        ["76102", "76107", "76109", "76116", "76010", "76011", "76012", "76013", "76015"],
        "95.00",
        "75.00",
    ),
]

# name, slug, price, [(item name, variant name, quantity)]
PACKAGES = [
    (
        "Kids birthday bash",
        "kids-birthday-bash",
        "249.00",
        [
            ("Bounce house", "Default", 1),
            ("Kids table", "Default", 2),
            ("Kids chair", "Default", 12),
            ("Popcorn machine", "Default", 1),
        ],
    ),
    (
        "Wedding reception starter",
        "wedding-reception-starter",
        "649.00",
        [
            ("Round table", "Default", 6),
            ("Chiavari chair", "Gold", 48),
            ("Table covers and linen", "Default", 6),
            ("Photo backdrop", "Default", 1),
        ],
    ),
    (
        "Carnival treats duo",
        "carnival-treats-duo",
        "140.00",
        [
            ("Popcorn machine", "Default", 1),
            ("Cotton candy machine", "Default", 1),
            ("Slushy machine", "Default", 1),
        ],
    ),
    (
        "Corporate fun day",
        "corporate-fun-day",
        "725.00",
        [
            ("Golf simulator", "Default", 1),
            ("Canopy", "Default", 2),
            ("Rectangle table", "Default", 4),
            ("Garden chair", "Black", 20),
        ],
    ),
]

S = Booking.Status


@dataclass
class DemoBooking:
    """One demo booking: when, where, what it holds, and the money that has moved."""

    key: str
    name: str
    day: int
    status: str
    city: str
    zip_code: str
    address: str
    event_type: str
    lines: list
    zone: str
    delivery: tuple
    pickup: tuple
    notes: str = ""
    surface: str = Surface.GRASS
    packages: list = field(default_factory=list)
    paid: str = "none"  # none, deposit, full, balance_failed
    method: str = Payment.Method.CARD
    attention: bool = False
    override_note: str = ""
    organisation: bool = False


DEMO_BOOKINGS = [
    DemoBooking(
        "01",
        "Tasha Greene",
        0,
        S.RETURNED,
        "Dallas",
        "75206",
        "4210 Mockingbird Ln",
        EventType.BIRTHDAY,
        [("Combo bounce house", "Default", 1)],
        "Dallas core",
        (time(9, 0), time(11, 0)),
        (time(13, 0), None),
        "Gate code 4471. Park in the alley.",
        paid="full",
    ),  # fmt: skip
    DemoBooking(
        "02",
        "Grace Community Church",
        0,
        S.DELIVERED,
        "Plano",
        "75074",
        "900 W 15th St",
        EventType.CHURCH,
        [
            ("Canopy", "Default", 4),
            ("Rectangle table", "Default", 10),
            ("Garden chair", "White", 80),
        ],
        "North DFW (Plano, Frisco, McKinney)",
        (time(8, 0), time(10, 0)),
        (time(17, 0), None),
        "Set up behind the fellowship hall. Ask for Pastor Dan.",
        paid="full",
        method=Payment.Method.ACH,
        organisation=True,
    ),  # fmt: skip
    DemoBooking(
        "03",
        "Marcus Bell",
        0,
        S.DELIVERED,
        "Dallas",
        "75208",
        "611 N Bishop Ave",
        EventType.CORPORATE,
        [],
        "Dallas core",
        (time(10, 0), time(12, 0)),
        (time(16, 0), None),
        "Balance was cash on arrival; not yet recorded.",
        surface=Surface.INDOOR,
        packages=[("corporate-fun-day", 1)],
        paid="deposit",
        attention=True,
        override_note="Delivered with balance outstanding; owner agreed to cash on the day.",
    ),  # fmt: skip
    DemoBooking(
        "04",
        "Priya Nair",
        2,
        S.PAID,
        "Frisco",
        "75034",
        "7201 Preston Rd",
        EventType.BIRTHDAY,
        [],
        "North DFW (Plano, Frisco, McKinney)",
        (time(9, 30), time(11, 30)),
        (time(15, 30), None),
        "Backyard with a side gate. Dog stays inside.",
        packages=[("kids-birthday-bash", 1)],
        paid="full",
    ),  # fmt: skip
    DemoBooking(
        "05",
        "Elena Ruiz",
        4,
        S.DEPOSIT_PAID,
        "Fort Worth",
        "76107",
        "2900 W 7th St",
        EventType.WEDDING,
        [("Photo backdrop", "Default", 2), ("Generator", "Default", 1)],
        "West DFW (Fort Worth, Arlington)",
        (time(11, 0), time(14, 0)),
        (time(23, 0), None),
        "Venue loading dock is on the east side.",
        surface=Surface.CONCRETE,
        packages=[("wedding-reception-starter", 1)],
        paid="deposit",
        method=Payment.Method.ZELLE,
    ),  # fmt: skip
    DemoBooking(
        "06",
        "Jordan Hayes",
        5,
        S.HELD,
        "Dallas",
        "75204",
        "2515 McKinney Ave",
        EventType.BIRTHDAY,
        [("Bounce house", "Default", 1), ("Popcorn machine", "Default", 1)],
        "Dallas core",
        (time(10, 0), time(12, 0)),
        (time(16, 0), None),
        "Apartment courtyard.",
        surface=Surface.CONCRETE,
    ),  # fmt: skip
    DemoBooking(
        "07",
        "Lincoln Elementary PTA",
        7,
        S.PAID,
        "Arlington",
        "76010",
        "1400 Sherry St",
        EventType.SCHOOL,
        [
            ("Water slide", "Default", 1),
            ("Obstacle course", "Default", 1),
            ("Cotton candy machine", "Default", 1),
            ("Generator", "Default", 1),
        ],
        "West DFW (Fort Worth, Arlington)",
        (time(7, 30), time(9, 0)),
        (time(15, 30), None),
        "School field day. Check in at the front office.",
        paid="full",
        method=Payment.Method.CASHAPP,
        organisation=True,
    ),  # fmt: skip
    DemoBooking(
        "08",
        "Naomi Okafor",
        8,
        S.DEPOSIT_PAID,
        "McKinney",
        "75069",
        "1820 Virginia Pkwy",
        EventType.BIRTHDAY,
        [("Combo bounce house", "Default", 1), ("Kids table", "Default", 3)],
        "North DFW (Plano, Frisco, McKinney)",
        (time(9, 0), time(11, 0)),
        (time(14, 0), None),
        "Please set up in the front yard, away from the pool.",
        paid="deposit",
    ),  # fmt: skip
    DemoBooking(
        "09",
        "Samir Patel",
        10,
        S.HELD,
        "Richardson",
        "75080",
        "310 W Arapaho Rd",
        EventType.CORPORATE,
        [("Golf simulator", "Default", 1), ("Canopy", "Default", 2)],
        "North DFW (Plano, Frisco, McKinney)",
        (time(9, 0), time(11, 30)),
        (time(17, 30), None),
        "Loading dock behind the building.",
        surface=Surface.CONCRETE,
    ),  # fmt: skip
    DemoBooking(
        "10",
        "Hope Fellowship Church",
        12,
        S.DEPOSIT_PAID,
        "Dallas",
        "75214",
        "6300 E Mockingbird Ln",
        EventType.CHURCH,
        [
            ("Canopy", "Default", 3),
            ("Round table", "Default", 12),
            ("Garden chair", "Black", 90),
            ("Popcorn machine", "Default", 1),
            ("Slushy machine", "Default", 1),
        ],
        "Dallas core",
        (time(7, 0), time(9, 0)),
        (time(14, 0), None),
        "Church picnic on the east lawn.",
        paid="deposit",
        method=Payment.Method.BANK_DEPOSIT,
        organisation=True,
    ),  # fmt: skip
    DemoBooking(
        "11",
        "Amara and David Cole",
        14,
        S.PAID,
        "Dallas",
        "75205",
        "3300 Armstrong Ave",
        EventType.WEDDING,
        [
            ("Chiavari chair", "Silver", 40),
            ("Round table", "Default", 5),
            ("Table covers and linen", "Default", 5),
            ("Photo backdrop", "Default", 1),
        ],
        "Dallas core",
        (time(10, 0), time(13, 0)),
        (time(22, 30), None),
        "Garden ceremony. Venue coordinator will meet the crew.",
        paid="full",
    ),  # fmt: skip
    DemoBooking(
        "12",
        "Kevin Mallory",
        15,
        S.DEPOSIT_PAID,
        "Arlington",
        "76012",
        "2201 N Collins St",
        EventType.COMMUNITY,
        [("Margarita machine", "Default", 1), ("Slushy machine", "Default", 1)],
        "West DFW (Fort Worth, Arlington)",
        (time(12, 0), time(14, 0)),
        (time(21, 0), None),
        "Block party. Machines need a dedicated outlet.",
        paid="balance_failed",
        attention=True,
    ),  # fmt: skip
    DemoBooking(
        "13",
        "Rachel Kim",
        17,
        S.HELD,
        "Plano",
        "75024",
        "5800 Legacy Dr",
        EventType.BIRTHDAY,
        [("Water slide", "Default", 1), ("Cotton candy machine", "Default", 1)],
        "North DFW (Plano, Frisco, McKinney)",
        (time(10, 0), time(12, 0)),
        (time(17, 0), None),
        "Waiting on the deposit; hold ends soon.",
    ),  # fmt: skip
    DemoBooking(
        "14",
        "Oak Cliff Youth League",
        19,
        S.PAID,
        "Dallas",
        "75208",
        "1200 W Jefferson Blvd",
        EventType.COMMUNITY,
        [
            ("Obstacle course", "Default", 1),
            ("Bounce house", "Default", 2),
            ("Generator", "Default", 2),
        ],
        "Dallas core",
        (time(8, 0), time(10, 0)),
        (time(16, 0), None),
        "Season kickoff. Field behind the rec centre.",
        paid="full",
        organisation=True,
    ),  # fmt: skip
    DemoBooking(
        "15",
        "Diane Foster",
        20,
        S.DEPOSIT_PAID,
        "Fort Worth",
        "76109",
        "4801 Bryce Ave",
        EventType.OTHER,
        [
            ("Kids table", "Default", 4),
            ("Kids chair", "Default", 24),
            ("Popcorn machine", "Default", 1),
        ],
        "West DFW (Fort Worth, Arlington)",
        (time(11, 0), time(13, 0)),
        (time(18, 0), None),
        "Baby shower, indoors. Call on arrival.",
        surface=Surface.INDOOR,
        paid="deposit",
    ),  # fmt: skip
]


def _money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))


def seed_prices():
    """Price every seeded item and confirm its stock. Returns the number of variants set."""
    count = 0
    for item_name, (price, caution, stock) in PRICING.items():
        item = Item.objects.get(name=item_name)
        item.base_price = _money(price)
        item.caution_fee = _money(caution)
        item.save(update_fields=["base_price", "caution_fee", "updated_at"])
        for variant_name, quantity in stock.items():
            ItemVariant.objects.filter(item=item, name=variant_name).update(
                stock_quantity=quantity, stock_confirmed=True, price_override=None
            )
            count += 1
    return count


def seed_zones():
    """Create or reset the three delivery zones."""
    for name, zips, delivery_fee, setup_fee in ZONES:
        DeliveryZone.objects.update_or_create(
            name=name,
            defaults={
                "zip_codes": zips,
                "delivery_fee": _money(delivery_fee),
                "setup_fee": _money(setup_fee),
                "is_active": True,
            },
        )
    return len(ZONES)


def seed_packages():
    """Create or reset the four packages and their contents."""
    for name, slug, price, contents in PACKAGES:
        package, _ = Package.objects.update_or_create(
            slug=slug, defaults={"name": name, "package_price": _money(price), "is_active": True}
        )
        keep = []
        for item_name, variant_name, quantity in contents:
            variant = ItemVariant.objects.get(item__name=item_name, name=variant_name)
            line, _ = PackageItem.objects.update_or_create(
                package=package, variant=variant, defaults={"quantity": quantity}
            )
            keep.append(line.pk)
        package.items.exclude(pk__in=keep).delete()
    return len(PACKAGES)


def seed_users():
    """Create the Owner and Crew logins if missing. Returns how many were created."""
    user_model = get_user_model()
    created = 0
    for username, group_name, is_superuser in (
        (OWNER_USERNAME, roles.OWNER, False),
        (CREW_USERNAME, roles.CREW, False),
    ):
        user, was_created = user_model.objects.get_or_create(
            username=username,
            defaults={"email": f"{username}@{DEMO_EMAIL_DOMAIN}", "is_staff": True},
        )
        if was_created:
            user.set_password(DEMO_PASSWORD)
            user.save()
            created += 1
        user.is_staff = True
        user.is_superuser = is_superuser
        user.save(update_fields=["is_staff", "is_superuser"])
        user.groups.set([Group.objects.get(name=group_name)])
    return created


def _payment(booking, kind, amount, method, status, when, reference, staff):
    manual = method in Payment.MANUAL_METHODS
    Payment.objects.create(
        booking=booking,
        amount=amount,
        method=method,
        kind=kind,
        status=status,
        reference=reference,
        received_by=staff if manual else None,
        received_at=when if status == Payment.Status.SUCCEEDED else None,
        failure_reason=Payment.FailureReason.STRIPE_FAILED if status == "failed" else "",
    )


def _record_payments(booking, spec, staff, now):
    """Write the payments a booking's demo story calls for."""
    if spec.paid == "none":
        return
    total, deposit = booking.total, booking.deposit_amount
    method = spec.method
    ref = f"DEMO-{spec.key}"
    manual_ref = f"{method.upper()}-{spec.key}" if method in Payment.MANUAL_METHODS else ref
    if spec.paid == "full":
        _payment(
            booking,
            Payment.Kind.FULL,
            total,
            method,
            Payment.Status.SUCCEEDED,
            now - timedelta(days=3),
            manual_ref,
            staff,
        )
    else:
        _payment(
            booking,
            Payment.Kind.DEPOSIT,
            deposit,
            method,
            Payment.Status.SUCCEEDED,
            now - timedelta(days=2),
            manual_ref,
            staff,
        )
    if spec.paid == "balance_failed":
        _payment(
            booking,
            Payment.Kind.BALANCE,
            total - deposit,
            Payment.Method.CARD,
            Payment.Status.FAILED,
            now - timedelta(hours=6),
            f"{ref}-BAL",
            staff,
        )


def _create_lines(booking, spec):
    """Add the booking's loose items and packages at today's prices; return the subtotal."""
    quantities = {}
    prices = {}
    subtotal = Decimal("0.00")
    for item_name, variant_name, quantity in spec.lines:
        variant = ItemVariant.objects.select_related("item").get(
            item__name=item_name, name=variant_name
        )
        quantities[variant.pk] = quantities.get(variant.pk, 0) + quantity
        prices[variant.pk] = variant.unit_price
        subtotal += variant.unit_price * quantity
    package_units = {}
    for slug, quantity in spec.packages:
        package = Package.objects.prefetch_related("items").get(slug=slug)
        BookingPackage.objects.create(
            booking=booking,
            package=package,
            name=package.name,
            quantity=quantity,
            unit_price=package.package_price,
        )
        subtotal += package.package_price * quantity
        for line in package.items.all():
            variant = line.variant
            quantities[variant.pk] = quantities.get(variant.pk, 0) + line.quantity * quantity
            prices[variant.pk] = variant.unit_price
            package_units[variant.pk] = package_units.get(variant.pk, 0) + line.quantity * quantity
    for variant_id, quantity in quantities.items():
        BookingItem.objects.create(
            booking=booking,
            variant_id=variant_id,
            quantity=quantity,
            unit_price=prices[variant_id],
            package_quantity=package_units.get(variant_id, 0),
        )
    return subtotal


def _create_booking(spec, today, now, staff):
    zone = DeliveryZone.objects.get(name=spec.zone)
    event_date = today + timedelta(days=spec.day)
    delivery_time, event_start = spec.delivery
    pickup_time, _ = spec.pickup
    booking = Booking.objects.create(
        customer_name=spec.name,
        email=f"booking{spec.key}@{DEMO_EMAIL_DOMAIN}",
        phone=f"469-555-01{spec.key}",
        event_date=event_date,
        delivery_address=f"{spec.address}, {spec.city}, TX {spec.zip_code}",
        city=spec.city,
        zip_code=spec.zip_code,
        status=spec.status,
        hold_expires_at=now + timedelta(hours=24) if spec.status == S.HELD else None,
        delivery_fee=zone.delivery_fee,
        setup_fee=zone.setup_fee,
        event_type=spec.event_type,
        surface=spec.surface,
        crew_notes=spec.notes,
        event_start_time=event_start,
        delivery_time=delivery_time,
        setup_by_time=event_start,
        pickup_date=event_date,
        pickup_time=pickup_time,
        payment_choice=spec.method if spec.method != Payment.Method.ACH else "stripe",
        payment_plan="full" if spec.paid == "full" else "deposit",
        terms_version=settings.TERMS_VERSION,
        terms_accepted_at=now - timedelta(days=4),
        needs_attention=spec.attention,
        delivery_override_note=spec.override_note,
        delivery_override_by=staff if spec.override_note else None,
        delivery_override_at=now if spec.override_note else None,
    )
    subtotal = _create_lines(booking, spec)
    booking.tax = _tax(subtotal, zone)
    booking.caution_fee = sum(
        (
            line.variant.item.caution_fee * line.quantity
            for line in booking.items.select_related("variant__item")
        ),
        Decimal("0.00"),
    )
    booking.save(update_fields=["tax", "caution_fee", "updated_at"])
    _record_payments(booking, spec, staff, now)
    return booking


def _tax(subtotal, zone):
    return sales_tax(subtotal, Decimal("0.00"), zone.delivery_fee, zone.setup_fee)


@transaction.atomic
def seed_demo():
    """Seed everything the demo needs. Returns counts of what was set or created."""
    seed_catalogue()
    users_before = get_user_model().objects.count()
    result = {
        "variants": seed_prices(),
        "zones": seed_zones(),
        "packages": seed_packages(),
        "users": 0,
        "bookings": 0,
    }
    seed_users()
    result["users"] = get_user_model().objects.count() - users_before
    staff = get_user_model().objects.get(username=OWNER_USERNAME)
    now = timezone.now()
    today = timezone.localdate()
    for spec in DEMO_BOOKINGS:
        if Booking.objects.filter(email=f"booking{spec.key}@{DEMO_EMAIL_DOMAIN}").exists():
            continue
        _create_booking(spec, today, now, staff)
        result["bookings"] += 1
    return result
