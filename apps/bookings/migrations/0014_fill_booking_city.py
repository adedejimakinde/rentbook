"""Fill Booking.city from the saved delivery address for bookings made before it existed.

The booking flow saved addresses as "street, newline, City, ST ZIP".
"""

import re

from django.db import migrations

CITY_LINE = re.compile(r"^(?P<city>[^,]+),\s*[A-Za-z]{2}\b")


def city_from_address(address):
    lines = [line.strip() for line in (address or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return ""
    found = CITY_LINE.match(lines[-1])
    return found.group("city").strip()[:100] if found else ""


def fill_city(apps, schema_editor):
    booking = apps.get_model("bookings", "Booking")
    for row in booking.objects.filter(city="").only("pk", "delivery_address"):
        city = city_from_address(row.delivery_address)
        if city:
            booking.objects.filter(pk=row.pk).update(city=city)


class Migration(migrations.Migration):
    dependencies = [("bookings", "0013_booking_city_and_log")]

    operations = [migrations.RunPython(fill_city, migrations.RunPython.noop)]
