"""The crew schedule for one day, and the mark delivered and returned actions.

Marking goes through the booking lifecycle services only, so the override
rules (a note to deliver with money owed, who gave it, the flag on the booking)
are the same as in the admin.
"""

from dataclasses import dataclass, field

from django.db.models import Prefetch, Q

from apps.bookings.models import Booking, BookingItem
from apps.bookings.services import lifecycle
from apps.dashboard.services import stops as stop_service
from apps.dashboard.services.money import with_money
from apps.dashboard.services.visibility import visibility_for
from apps.payments.services import ledger


@dataclass
class Card:
    """One stop with everything the crew needs, already filtered for the viewer."""

    stop: stop_service.Stop
    city: str
    items: list
    power_items: list
    show_phone: bool
    show_money: bool
    balance: object = None
    caution_held: object = None
    message: str = ""
    needs_override: bool = False
    checklist: list = field(default_factory=list)

    @property
    def booking(self):
        return self.stop.booking


def item_lines(booking):
    """(quantity, name) for every line, with the variant named only when it adds something."""
    lines = []
    for line in booking.items.all():
        variant = line.variant
        name = (
            variant.item.name
            if variant.name == "Default"
            else f"{variant.item.name} ({variant.name})"
        )
        lines.append((line.quantity, name))
    return lines


def _power_items(booking):
    return [line.variant.item.name for line in booking.items.all() if line.variant.item.needs_power]


def make_card(stop, cities, visible):
    """Build the card for one stop, leaving out what the viewer may not see."""
    booking = stop.booking
    card = Card(
        stop=stop,
        city=cities.city(booking),
        items=item_lines(booking),
        power_items=_power_items(booking),
        show_phone=visible.show_phone,
        show_money=visible.show_money,
    )
    if visible.show_money:
        card.balance = booking.m_balance
        card.caution_held = booking.m_caution_held
    return card


def schedule(day, user):
    """The cards for day, in crew order, for this user."""
    bookings = list(
        with_money(
            Booking.objects.filter(status__in=stop_service.STOP_STATUSES).filter(
                Q(event_date=day) | Q(pickup_date=day)
            )
        )
        .prefetch_related(Prefetch("items", BookingItem.objects.select_related("variant__item")))
        .order_by("pk")
    )
    visible = visibility_for(user)
    cities = stop_service.CityLookup()
    return [make_card(stop, cities, visible) for stop in stop_service.stops_on(day, bookings)]


def card_for(booking_id, kind, day, user):
    """Rebuild one stop's card after an action, or None if it is no longer a stop that day."""
    for card in schedule(day, user):
        if card.booking.pk == booking_id and card.stop.kind == kind:
            return card
    return None


@dataclass
class MarkResult:
    ok: bool
    message: str = ""
    needs_override: bool = False


def mark_delivered(booking_id, *, by, show_money, override_note=""):
    """Mark a booking delivered through the lifecycle service.

    When money or a payment is outstanding and no override note was given, the
    result asks for one. Crew who may not see money are told only that there is
    something outstanding, never the amounts.
    """
    booking = Booking.objects.get(pk=booking_id)
    try:
        lifecycle.mark_delivered(booking, by=by, override_note=override_note)
    except lifecycle.BookingStateError as error:
        booking.refresh_from_db()
        blockers = (
            lifecycle.delivery_blockers(booking)
            if booking.status in ledger.SECURED_STATUSES
            else []
        )
        if blockers and not override_note.strip():
            if show_money:
                return MarkResult(False, str(error), needs_override=True)
            return MarkResult(
                False,
                "Money or a payment is still outstanding on this booking. Add a note to "
                "deliver anyway, or call the office.",
                needs_override=True,
            )
        return MarkResult(False, str(error))
    return MarkResult(True, "Marked delivered.")


def mark_returned(booking_id):
    """Mark a delivered booking returned through the lifecycle service."""
    booking = Booking.objects.get(pk=booking_id)
    try:
        lifecycle.mark_returned(booking)
    except lifecycle.BookingStateError as error:
        return MarkResult(False, str(error))
    return MarkResult(True, "Marked returned.")
