"""The online booking flow: details kept between steps, and turning the cart into a booking.

Nothing here prices or reserves stock itself. The booking is created only by
quote.create_booking_from_quote, which rebuilds the quote from the cart and
reserves the stock under lock; this module then records the customer's event
details, terms acceptance and payment choice on it.
"""

from datetime import time, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.bookings.models import MANUAL_PAYMENT_CHOICES, Booking, PaymentPlan
from apps.bookings.services import lifecycle
from apps.bookings.services import quote as quote_service
from apps.bookings.services.cart import line_requests
from apps.payments.models import Payment
from apps.payments.services import ledger
from apps.payments.services import stripe as stripe_service

FLOW_KEY = "booking_flow"
EVENT, DELIVERY, CONTACT, REVIEW = "event", "delivery", "contact", "review"
STEPS = (EVENT, DELIVERY, CONTACT, REVIEW)
STEP_TITLES = {
    EVENT: "Event details",
    DELIVERY: "Delivery",
    CONTACT: "Contact",
    REVIEW: "Review and pay",
}
PENDING_BOOKING_KEY = "pending_booking"

PLAN_KINDS = {PaymentPlan.DEPOSIT: Payment.Kind.DEPOSIT, PaymentPlan.FULL: Payment.Kind.FULL}


def get_flow(request):
    """The details entered so far, keyed by step."""
    return request.session.get(FLOW_KEY, {})


def save_step(request, step, data):
    """Store a step's cleaned data (JSON-safe values only)."""
    flow = dict(get_flow(request))
    flow[step] = data
    request.session[FLOW_KEY] = flow


def first_incomplete_step(request):
    """The earliest step still to fill in, or REVIEW when every detail step is done."""
    flow = get_flow(request)
    for step in STEPS[:-1]:
        if step not in flow:
            return step
    return REVIEW


def clear_flow(request):
    request.session.pop(FLOW_KEY, None)
    request.session.pop(PENDING_BOOKING_KEY, None)


def format_address(delivery):
    """The delivery address as one block of text, as staff see it."""
    return f"{delivery['street']}\n{delivery['city']}, {delivery['state']} {delivery['zip_code']}"


def is_manual(choice):
    return choice in MANUAL_PAYMENT_CHOICES


def release_pending(request):
    """Cancel this visitor's earlier unpaid online booking before they submit again.

    A customer who backs out of Stripe Checkout and submits again would
    otherwise hold the same stock twice. Only a booking this session created,
    still held with nothing paid, is cancelled; its open checkout is expired
    at Stripe first. Returns a booking that has money on it or clearing, so
    the caller can send the customer to it instead of booking again.
    """
    booking_id = request.session.pop(PENDING_BOOKING_KEY, None)
    if booking_id is None:
        return None
    booking = Booking.objects.filter(pk=booking_id).first()
    if booking is None or booking.status != Booking.Status.HELD:
        return booking if booking and booking.status in ledger.SECURED_STATUSES else None
    try:
        stripe_service.close_open_checkouts(booking)
    except ledger.PaymentError:
        return booking
    booking.refresh_from_db()
    if booking.amount_paid > 0 or booking.payments.in_progress().exists():
        return booking
    try:
        lifecycle.cancel_booking(booking)
    except lifecycle.BookingStateError:
        return booking
    return None


def submit_booking(lines, *, event_date, zip_code, promo_code, flow, plan, choice, now=None):
    """Create the held booking for the cart and record the flow's details on it.

    Raises quote.QuoteNotBookable when the rebuilt quote has errors (the cart
    changed, stock sold out, the ZIP is not served). A booking to be paid by
    Zelle, Cash App or bank deposit is held MANUAL_PAY_HOLD_HOURS so the money
    has time to arrive; card and ACH bookings keep the normal hold, which
    create_checkout_session extends while Stripe Checkout is open.
    """
    now = now or timezone.now()
    event, delivery, contact = flow[EVENT], flow[DELIVERY], flow[CONTACT]
    with transaction.atomic():
        booking = quote_service.create_booking_from_quote(
            line_requests(lines),
            event_date,
            zip_code,
            promo_code or None,
            customer_name=contact["name"],
            email=contact["email"],
            phone=contact["phone"],
            delivery_address=format_address(delivery),
        )
        booking.event_start_time = time.fromisoformat(event["start_time"])
        booking.setup_by_time = time.fromisoformat(event["setup_by_time"])
        booking.event_type = event["event_type"]
        booking.surface = delivery["surface"]
        booking.crew_notes = delivery["crew_notes"]
        booking.tax_exempt_requested = contact["tax_exempt"]
        booking.tax_exempt_org_name = contact["org_name"] if contact["tax_exempt"] else ""
        booking.tax_exempt_cert_number = contact["cert_number"] if contact["tax_exempt"] else ""
        booking.terms_version = settings.TERMS_VERSION
        booking.terms_accepted_at = now
        booking.payment_choice = choice
        booking.payment_plan = plan
        fields = [
            "event_start_time",
            "setup_by_time",
            "event_type",
            "surface",
            "crew_notes",
            "tax_exempt_requested",
            "tax_exempt_org_name",
            "tax_exempt_cert_number",
            "terms_version",
            "terms_accepted_at",
            "payment_choice",
            "payment_plan",
            "updated_at",
        ]
        if is_manual(choice):
            booking.hold_expires_at = now + timedelta(hours=settings.MANUAL_PAY_HOLD_HOURS)
            fields.append("hold_expires_at")
        booking.save(update_fields=fields)
    return booking


def remember_pending(request, booking):
    """Note the booking this session sent to Stripe, until payment is confirmed."""
    request.session[PENDING_BOOKING_KEY] = booking.pk


def amount_to_pay(booking, plan):
    """(rental amount, caution) the customer should send now, from the payment service."""
    return stripe_service.amount_due(booking, PLAN_KINDS[plan])
