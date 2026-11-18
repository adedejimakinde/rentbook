"""Delivery and pickup stops: what the crew does on a given day.

A delivery is on the event date; a pickup is on the pickup date (the event date
unless staff set another). Both are listed in time order, and stops without a
time come last.
"""

from dataclasses import dataclass
from datetime import time
from urllib.parse import quote_plus

from apps.bookings.models import Booking, DeliveryZone

DELIVERY = "delivery"
PICKUP = "pickup"
STOP_STATUSES = (
    Booking.Status.DEPOSIT_PAID,
    Booking.Status.PAID,
    Booking.Status.DELIVERED,
    Booking.Status.RETURNED,
)
DONE_STATUSES = {DELIVERY: (Booking.Status.DELIVERED, Booking.Status.RETURNED)}


@dataclass
class Stop:
    """One delivery or pickup for one booking on one day."""

    booking: Booking
    kind: str

    @property
    def time(self):
        """The planned arrival time, or None until staff set it."""
        planned = self.booking.delivery_time if self.kind == DELIVERY else self.booking.pickup_time
        return planned

    @property
    def sort_key(self):
        if self.time:
            return (0, self.time, self.booking.pk)
        return (1, self.booking.setup_by_time or time.max, self.booking.pk)

    @property
    def time_label(self):
        if self.time:
            return f"{self.time:%-I:%M %p}"
        if self.kind == DELIVERY and self.booking.setup_by_time:
            return f"Setup by {self.booking.setup_by_time:%-I:%M %p}"
        return "Time TBC"

    @property
    def is_delivery(self):
        return self.kind == DELIVERY

    @property
    def done(self):
        """True once the crew has marked this stop done."""
        if self.kind == DELIVERY:
            return self.booking.status in (Booking.Status.DELIVERED, Booking.Status.RETURNED)
        return self.booking.status == Booking.Status.RETURNED

    @property
    def kind_label(self):
        return "Delivery" if self.kind == DELIVERY else "Pickup"

    @property
    def maps_url(self):
        address = " ".join(self.booking.delivery_address.split())
        return f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}"


def stops_on(day, bookings):
    """The stops on day among bookings, in crew order."""
    stops = []
    for booking in bookings:
        if booking.status not in STOP_STATUSES:
            continue
        if booking.event_date == day:
            stops.append(Stop(booking, DELIVERY))
        if booking.effective_pickup_date == day:
            stops.append(Stop(booking, PICKUP))
    return sorted(stops, key=lambda stop: stop.sort_key)


class CityLookup:
    """The city for a booking: the one saved on it, else its delivery zone's name."""

    def __init__(self):
        self._zones = None

    def _zone_names(self):
        if self._zones is None:
            self._zones = {}
            for zone in DeliveryZone.objects.order_by("-is_active", "name"):
                for zip_code in zone.zip_codes:
                    self._zones.setdefault(zip_code, zone.name)
        return self._zones

    def city(self, booking):
        return booking.city or self._zone_names().get(booking.zip_code, "")
