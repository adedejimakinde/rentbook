"""The frozen copy of a booking's lines and money that an invoice or receipt is printed from.

Everything is stored as plain JSON (money as "12.50" strings) so the document
reads the same whatever later happens to the booking, its prices or settings.
"""

from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from apps.bookings.models import Booking
from apps.payments.models import Payment
from apps.payments.services import manual as manual_service

SNAPSHOT_VERSION = 1
ZERO = Decimal("0.00")


def money(value):
    """A Decimal as a two-place string, the only form money takes in a snapshot."""
    return str(Decimal(value or 0).quantize(Decimal("0.01")))


def _iso(value):
    return value.isoformat() if value else None


def _lines(booking):
    items = [
        {
            "name": str(item.variant),
            "quantity": item.quantity - item.package_quantity,
            "unit_price": money(item.unit_price),
            "amount": money((item.quantity - item.package_quantity) * item.unit_price),
        }
        for item in booking.items.all()
        if item.quantity > item.package_quantity
    ]
    packages = [
        {
            "name": package.name,
            "quantity": package.quantity,
            "unit_price": money(package.unit_price),
            "amount": money(package.quantity * package.unit_price),
        }
        for package in booking.packages.all()
    ]
    return items, packages


def _payment(payment):
    return {
        "id": payment.pk,
        "kind": payment.get_kind_display(),
        "method": payment.get_method_display() or "",
        "amount": money(payment.amount),
        "caution_amount": money(payment.caution_amount),
        "total": money(payment.charged_total),
        "reference": payment.reference,
        "received_at": _iso(payment.received_at or payment.created_at),
    }


def _refund(entry=None, *, amount=None, method="", reference="", refund_of=None):
    if entry is not None:
        return {
            "id": entry.pk,
            "kind": entry.get_kind_display(),
            "method": entry.get_method_display() or "",
            "amount": money(entry.amount),
            "reference": entry.reference,
            "refunded_at": _iso(entry.received_at or entry.created_at),
            "refund_of": entry.refund_of_id,
        }
    return {
        "id": None,
        "kind": Payment.Kind.RENTAL_REFUND.label,
        "method": method,
        "amount": money(amount),
        "reference": reference,
        "refunded_at": _iso(timezone.now()),
        "refund_of": refund_of,
    }


def build(booking, *, payment=None, refund=None):
    """The snapshot of booking as it stands now, with the payment or refund it is for."""
    booking = Booking.objects.prefetch_related("items__variant__item", "packages").get(
        pk=booking.pk
    )
    items, packages = _lines(booking)
    subtotal = sum((Decimal(line["amount"]) for line in items + packages), ZERO)
    data = {
        "version": SNAPSHOT_VERSION,
        "business": {
            "name": settings.BUSINESS_NAME,
            "tagline": settings.BUSINESS_TAGLINE,
            "area": settings.BUSINESS_AREA,
            "phone": settings.BUSINESS_PHONE,
            "email": settings.BUSINESS_EMAIL,
            "website": settings.SITE_URL,
        },
        "booking": {
            "id": booking.pk,
            "reference": f"Booking #{booking.pk}",
            "status": booking.get_status_display(),
            "payment_choice": booking.payment_choice,
            "payment_choice_label": booking.get_payment_choice_display()
            if booking.payment_choice
            else "",
            "payment_plan": booking.payment_plan,
            "hold_expires_at": _iso(booking.hold_expires_at),
        },
        "billed_to": {
            "name": booking.customer_name,
            "email": booking.email,
            "phone": booking.phone,
        },
        "event": {
            "date": booking.event_date.isoformat(),
            "start_time": booking.event_start_time.strftime("%H:%M")
            if booking.event_start_time
            else None,
            "address": booking.delivery_address,
        },
        "items": items,
        "packages": packages,
        "subtotal": money(subtotal),
        "discount": money(booking.discount),
        "promo_code": booking.promo_code.code if booking.promo_code_id else "",
        "delivery_fee": money(booking.delivery_fee),
        "setup_fee": money(booking.setup_fee),
        "tax": money(booking.tax),
        "tax_rate": str(settings.SALES_TAX_RATE),
        "tax_exempt": booking.tax_exempt,
        "tax_exempt_certificate": booking.tax_exempt_certificate,
        "total": money(booking.total),
        "deposit_amount": money(booking.deposit_amount),
        "caution_fee": money(booking.caution_fee),
        "caution_held": money(booking.caution_held),
        "caution_due": money(booking.caution_due),
        "amount_paid": money(booking.amount_paid),
        "balance_due": money(booking.balance_due),
        "overpaid": money(booking.overpaid),
        "payment_memo": manual_service.payment_memo(booking),
        "payment_instructions": manual_service.instructions(),
        "payment": _payment(payment) if payment is not None else None,
        "refund": refund,
    }
    return data


def refund_details(entry=None, **kwargs):
    """The refund part of a refund receipt's snapshot: from an entry, or a Stripe refund."""
    return _refund(entry, **kwargs)
