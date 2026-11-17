"""Helpers for invoice and email tests."""

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.bookings.models import Booking, PaymentChoice, PaymentPlan
from apps.bookings.services.quote import LineRequest, create_booking_from_quote
from apps.bookings.tests.factories import DeliveryZoneFactory, ItemVariantFactory
from apps.invoices.models import Invoice
from apps.invoices.services import pdf

ZIP = "75001"
CUSTOMER = {
    "customer_name": "Ada Planner",
    "email": "ada@example.com",
    "phone": "555-0100",
    "delivery_address": "1 Main St\nDallas, TX 75001",
}


def online_booking(
    event_date,
    *,
    price="25.00",
    quantity=4,
    caution="0.00",
    choice=PaymentChoice.STRIPE,
    plan=PaymentPlan.DEPOSIT,
):
    """A booking made through create_booking_from_quote: 4 chairs, $40 delivery, $20 setup.

    Its payment choice is recorded the way the booking flow records it; a
    manual choice gets the 24-hour hold.
    """
    DeliveryZoneFactory(zip_codes=[ZIP], delivery_fee=Decimal("40.00"), setup_fee=Decimal("20.00"))
    variant = ItemVariantFactory(
        item__name="Gold Chiavari Chair",
        item__base_price=Decimal(price),
        item__caution_fee=Decimal(caution),
        stock_quantity=100,
    )
    booking = create_booking_from_quote(
        [LineRequest(variant_id=variant.pk, quantity=quantity)], event_date, ZIP, **CUSTOMER
    )
    booking.payment_choice = choice
    booking.payment_plan = plan
    booking.terms_accepted_at = timezone.now()
    if choice != PaymentChoice.STRIPE:
        booking.hold_expires_at = timezone.now() + timedelta(hours=24)
    booking.save()
    return Booking.objects.get(pk=booking.pk)


def documents(booking, kind=None):
    invoices = Invoice.objects.filter(booking=booking).order_by("pk")
    return list(invoices.filter(kind=kind) if kind else invoices)


def pdf_text(invoice):
    """Every piece of text laid out in the invoice's PDF, with whitespace collapsed."""
    document = pdf.render_document(invoice)
    texts = []
    for page in document.pages:
        for box in page._page_box.descendants():
            text = getattr(box, "text", None)
            if isinstance(text, str):
                texts.append(text)
    return " ".join(" ".join(texts).split())
