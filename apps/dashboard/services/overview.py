"""Everything on the owner dashboard, from a handful of queries.

Read-only: nothing here changes data. Times are in the business time zone
(settings.TIME_ZONE), so "today" and "this month" roll over at local midnight.
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Exists, F, OuterRef, Q, Sum
from django.db.models.functions import Coalesce, Greatest, Least
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.services import availability
from apps.catalogue.models import ItemVariant
from apps.dashboard.services import stops as stop_service
from apps.dashboard.services.format import dollars
from apps.dashboard.services.money import MONEY, ZERO, with_money
from apps.dashboard.services.status import SCHEDULED_STATUSES, status_style
from apps.payments.models import Payment

ZERO_DOLLARS = Decimal("0.00")
OWED_STATUSES = (Booking.Status.DEPOSIT_PAID, Booking.Status.PAID)
UPCOMING_DAYS = 7
HOLD_WARNING = timedelta(hours=24)


def day_bounds(day):
    """The aware start of day and of the next day, in the business time zone."""
    zone = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(day, time.min), zone)
    return start, timezone.make_aware(datetime.combine(day + timedelta(days=1), time.min), zone)


def month_bounds(day):
    """First day of day's month and of the next month."""
    first = day.replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    return first, following


def greeting(now):
    hour = timezone.localtime(now).hour
    if hour < 12:
        return "Good morning"
    return "Good afternoon" if hour < 18 else "Good evening"


@dataclass
class JobRow:
    stop: stop_service.Stop
    city: str
    tags: list = field(default_factory=list)


@dataclass
class AttentionRow:
    booking: Booking
    reason: str


@dataclass
class UpcomingRow:
    booking: Booking
    label: str
    tone: str


@dataclass
class Dashboard:
    today: object
    now: datetime
    greeting: str
    summary: str
    received_month: Decimal
    still_owed: Decimal
    caution_held: Decimal
    week_count: int
    month_start: object
    month_after: object
    week_start: object
    week_end: object
    jobs: list
    attention: list
    expiring_holds: list
    stock_short: list
    upcoming: list


def _payment_totals(month_start, month_end):
    """Rental money received in the month, and caution money held, in one query."""
    start, _ = day_bounds(month_start)
    end, _ = day_bounds(month_end)
    rental_net = F("amount") - Least(F("refunded_amount"), F("amount"))
    caution_net = F("caution_amount") - Greatest(F("refunded_amount") - F("amount"), ZERO)
    received = Q(kind__in=Payment.RENTAL_KINDS) & Q(
        status__in=(Payment.Status.SUCCEEDED, Payment.Status.REFUNDED)
    )
    released = Q(kind__in=Payment.CAUTION_RELEASE_KINDS, status=Payment.Status.SUCCEEDED)
    totals = Payment.objects.annotate(counted_at=Coalesce("received_at", "created_at")).aggregate(
        received=Sum(
            rental_net,
            filter=received & Q(counted_at__gte=start, counted_at__lt=end),
            output_field=MONEY,
        ),
        caution_in=Sum(caution_net, filter=received, output_field=MONEY),
        caution_out=Sum("amount", filter=released, output_field=MONEY),
    )
    caution_held = (totals["caution_in"] or ZERO_DOLLARS) - (totals["caution_out"] or ZERO_DOLLARS)
    return totals["received"] or ZERO_DOLLARS, caution_held


def _still_owed(today):
    """What upcoming confirmed bookings still owe on their rental totals."""
    upcoming = with_money(Booking.objects.filter(event_date__gte=today, status__in=OWED_STATUSES))
    return upcoming.aggregate(owed=Sum("m_balance", output_field=MONEY))["owed"] or ZERO_DOLLARS


def _attention_reason(booking, short_ids):
    """Why a booking needs attention, where the data says so."""
    if booking.has_mismatch:
        return "Stripe charged a different amount. Resolve it."
    if booking.has_failed_refund:
        return "Refund failed at Stripe. Check and resend."
    if booking.pk in short_ids:
        return "Stock short after a damage report."
    if booking.delivery_override_note:
        return "Delivered with an override note. Check it."
    if booking.tax_exempt_requested and not booking.tax_exempt:
        return "Tax exemption requested. Verify certificate."
    return "Needs a look."


REFUND_KINDS = (
    Payment.Kind.RENTAL_REFUND,
    Payment.Kind.CAUTION_REFUND,
)


def job_tags(stop, *, show_money=True):
    """Status tags for one stop: (label, tone) pairs such as Paid, Collect $240, Inspect."""
    booking = stop.booking
    tags = []
    if stop.done:
        tags.append(("Delivered" if stop.is_delivery else "Returned", "muted"))
        return tags
    if show_money and booking.m_balance > 0:
        tags.append((f"Collect {dollars(booking.m_balance)}", "danger"))
    elif show_money:
        tags.append(("Paid", "success"))
    if not stop.is_delivery and booking.m_caution_held > 0:
        tags.append(("Inspect", "warning"))
    return tags


def short_bookings(today):
    """Upcoming bookings left short on stock by damage or stock changes."""
    return availability.bookings_left_short(
        ItemVariant.objects.filter(booking_items__booking__event_date__gte=today).values("pk"),
        today=today,
    )


def attention_rows(now=None):
    """The Needs attention rows alone (used to refresh the card after a booking is resolved)."""
    now = now or timezone.now()
    needing = [b for b in flagged_bookings(now) if b.needs_attention]
    short_ids = {b.pk for b in short_bookings(timezone.localdate(now))} if needing else set()
    return [AttentionRow(b, _attention_reason(b, short_ids)) for b in needing]


def flagged_bookings(now):
    """Bookings that need attention, or whose hold lapses within 24 hours, oldest first."""
    return list(
        Booking.objects.annotate(
            has_mismatch=Exists(
                Payment.objects.filter(
                    booking=OuterRef("pk"),
                    status=Payment.Status.PENDING,
                    mismatch_flagged_at__isnull=False,
                )
            ),
            has_failed_refund=Exists(
                Payment.objects.filter(
                    booking=OuterRef("pk"),
                    status=Payment.Status.FAILED,
                    kind__in=REFUND_KINDS,
                )
            ),
        )
        .filter(
            Q(needs_attention=True)
            | Q(
                status=Booking.Status.HELD,
                hold_expires_at__gt=now,
                hold_expires_at__lte=now + HOLD_WARNING,
            )
        )
        .order_by("updated_at", "pk")
    )


def build(now=None):
    """Gather the dashboard for the moment now."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    month_start, month_end = month_bounds(today)
    window_start = min(week_start, today)
    window_end = max(week_end, today + timedelta(days=UPCOMING_DAYS))

    received, caution_held = _payment_totals(month_start, month_end)
    still_owed = _still_owed(today)

    window = list(
        with_money(
            Booking.objects.filter(status__in=(*SCHEDULED_STATUSES,)).filter(
                Q(event_date__range=(window_start, window_end))
                | Q(pickup_date__range=(window_start, window_end))
            )
        ).order_by("event_date", "pk")
    )
    live = [b for b in window if is_live(b, now)]

    cities = stop_service.CityLookup()
    jobs = [
        JobRow(stop=stop, city=cities.city(stop.booking), tags=job_tags(stop))
        for stop in stop_service.stops_on(today, window)
    ]
    deliveries = sum(1 for job in jobs if job.stop.is_delivery)
    pickups = len(jobs) - deliveries
    on_site = {job.stop.booking.pk: job.stop.booking.m_balance for job in jobs if not job.stop.done}
    to_collect = sum((balance for balance in on_site.values() if balance > 0), ZERO_DOLLARS)

    flagged = flagged_bookings(now)
    needing = [b for b in flagged if b.needs_attention]
    expiring = sorted(
        (b for b in flagged if b.status == Booking.Status.HELD and b.hold_expires_at),
        key=lambda b: b.hold_expires_at,
    )
    short = short_bookings(today)
    short_ids = {b.pk for b in short}

    upcoming = [
        UpcomingRow(b, *status_style(b.status))
        for b in live
        if today < b.event_date <= today + timedelta(days=UPCOMING_DAYS)
    ][:10]
    week_count = sum(1 for b in live if week_start <= b.event_date <= week_end)

    return Dashboard(
        today=today,
        now=now,
        greeting=greeting(now),
        summary=_summary(deliveries, pickups, to_collect),
        received_month=received,
        still_owed=still_owed,
        caution_held=caution_held,
        week_count=week_count,
        month_start=month_start,
        month_after=month_end,
        week_start=week_start,
        week_end=week_end,
        jobs=jobs,
        attention=[AttentionRow(b, _attention_reason(b, short_ids)) for b in needing],
        expiring_holds=expiring,
        stock_short=short,
        upcoming=upcoming,
    )


def is_live(booking, now):
    """A held booking whose hold has lapsed no longer counts as a booking."""
    if booking.status != Booking.Status.HELD:
        return True
    return bool(booking.hold_expires_at and booking.hold_expires_at > now)


def _summary(deliveries, pickups, to_collect):
    """The one-line summary under the greeting."""
    if not deliveries and not pickups:
        return "No deliveries or pickups today."
    delivery_words = "1 delivery" if deliveries == 1 else f"{deliveries} deliveries"
    pickup_words = "1 pickup" if pickups == 1 else f"{pickups} pickups"
    line = f"{delivery_words} and {pickup_words} today."
    if to_collect > 0:
        line += f" {dollars(to_collect)} to collect on site."
    return line
