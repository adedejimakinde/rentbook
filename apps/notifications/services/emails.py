"""Customer and owner emails: what each says, who it goes to, and sending it once.

Each kind has a builder that returns an Email, or None when it should not be
sent (for example a booking received email for a booking already paid).
send() records the email's key in SentEmail in the same transaction as the
send, so a retried job never sends it twice, and a failed send leaves no
record and is retried.
"""

from dataclasses import dataclass, field

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import MANUAL_PAYMENT_CHOICES, Booking, PaymentChoice, PaymentPlan
from apps.bookings.services import booking_flow
from apps.invoices.models import Invoice
from apps.invoices.services import issuing, pdf
from apps.notifications.models import SentEmail
from apps.payments.models import Payment
from apps.payments.services import ledger
from apps.payments.services import manual as manual_service

PDF = "application/pdf"
NO_BALANCE_LINK = (Booking.Status.CANCELLED, Booking.Status.EXPIRED)


@dataclass
class Email:
    key: str
    kind: str
    booking: Booking
    to: list
    subject: str
    context: dict
    attachments: list = field(default_factory=list)


def _absolute(path):
    return f"{settings.SITE_URL}{path}"


def booking_context(booking):
    """Reference, links and business details every email shows."""
    urls = pdf.links_for(booking.pk)
    balance_due = booking.balance_due
    show_balance = balance_due > 0 and booking.status not in NO_BALANCE_LINK
    return {
        "booking": booking,
        "reference": f"Booking #{booking.pk}",
        "status_url": urls["status_url"],
        "balance_url": urls["balance_url"] if show_balance else "",
        "balance_due": balance_due,
        "business_name": settings.BUSINESS_NAME,
        "business_phone": settings.BUSINESS_PHONE,
        "business_email": settings.BUSINESS_EMAIL,
        "site_url": settings.SITE_URL,
        "logo_url": _absolute(static("brand/atglace-logo-header.png")),
        "font_regular_url": _absolute(static("fonts/montserrat-latin-500-normal.woff2")),
        "font_bold_url": _absolute(static("fonts/montserrat-latin-700-normal.woff2")),
    }


def _attachment(invoice):
    return (invoice.filename, pdf.render_pdf(invoice), PDF)


def _receipt(receipt_id):
    return Invoice.objects.select_related("booking", "payment").get(pk=receipt_id)


def _customer(kind, key, booking, subject, **context):
    return Email(
        key=key,
        kind=kind,
        booking=booking,
        to=[booking.email],
        subject=f"{subject}: Booking #{booking.pk}",
        context={**booking_context(booking), **context},
    )


def _owner(kind, key, booking, subject, **context):
    admin_path = reverse("admin:bookings_booking_change", args=[booking.pk])
    return Email(
        key=key,
        kind=kind,
        booking=booking,
        to=[settings.BUSINESS_EMAIL],
        subject=subject,
        context={
            **booking_context(booking),
            "balance_url": "",
            "admin_url": _absolute(admin_path),
            **context,
        },
    )


def build_booking_received(booking_id):
    """Sent when an online booking is made. Not sent once the booking is already secured."""
    booking = Booking.objects.get(pk=booking_id)
    if booking.status != Booking.Status.HELD:
        return None
    manual = booking.payment_choice in MANUAL_PAYMENT_CHOICES
    plan = booking.payment_plan or PaymentPlan.DEPOSIT
    try:
        amount, caution = booking_flow.amount_to_pay(booking, plan)
    except ledger.PaymentError:
        amount = caution = None
    context = {
        "manual": manual,
        "deadline": timezone.localtime(booking.hold_expires_at),
        "plan_label": PaymentPlan(plan).label,
        "amount": amount,
        "caution": caution,
        "to_send": amount + caution if amount is not None else None,
    }
    if manual:
        context.update(
            {
                "choice_label": PaymentChoice(booking.payment_choice).label,
                "details": manual_service.manual_details()[booking.payment_choice],
                "memo": manual_service.payment_memo(booking),
            }
        )
    return _customer(
        "booking_received",
        f"booking-received:{booking.pk}",
        booking,
        "We received your booking",
        **context,
    )


def build_booking_confirmed(receipt_id):
    """Sent for the payment that secured the booking: invoice and receipt attached."""
    receipt = _receipt(receipt_id)
    booking = receipt.booking
    email = _customer(
        "booking_confirmed",
        f"booking-confirmed:{receipt.payment_id}",
        booking,
        "Your booking is confirmed",
        receipt=receipt,
        s=receipt.snapshot,
    )
    invoice = issuing.latest_invoice(booking)
    if invoice is not None:
        email.attachments.append(_attachment(invoice))
    email.attachments.append(_attachment(receipt))
    return email


def build_payment_receipt(receipt_id):
    receipt = _receipt(receipt_id)
    email = _customer(
        "payment_receipt",
        f"receipt:{receipt.pk}",
        receipt.booking,
        "Payment received",
        receipt=receipt,
        s=receipt.snapshot,
    )
    email.attachments.append(_attachment(receipt))
    return email


def build_refund_receipt(receipt_id):
    receipt = _receipt(receipt_id)
    email = _customer(
        "refund_receipt",
        f"refund-receipt:{receipt.pk}",
        receipt.booking,
        "Your refund",
        receipt=receipt,
        s=receipt.snapshot,
    )
    email.attachments.append(_attachment(receipt))
    return email


def build_ach_processing(payment_id):
    """Sent once when an ACH bank payment starts clearing."""
    payment = Payment.objects.select_related("booking").get(pk=payment_id)
    if payment.status != Payment.Status.PENDING:
        return None
    booking = payment.booking
    return _customer(
        "ach_processing",
        f"ach-processing:{payment.pk}",
        booking,
        "Your bank payment is processing",
        payment=payment,
        ach_days=settings.ACH_HOLD_DAYS,
    )


def build_owner_new_booking(booking_id):
    booking = Booking.objects.prefetch_related("items__variant__item", "packages").get(
        pk=booking_id
    )
    return _owner(
        "owner_new_booking",
        f"owner-booking:{booking.pk}",
        booking,
        f"New booking #{booking.pk}: {booking.customer_name}, {booking.event_date:%b %d, %Y}",
        total=booking.total,
        caution_fee=booking.caution_fee,
    )


def build_owner_payment(receipt_id):
    receipt = _receipt(receipt_id)
    booking = receipt.booking
    payment = receipt.snapshot["payment"]
    return _owner(
        "owner_payment",
        f"owner-payment:{receipt.payment_id}",
        booking,
        f"Payment of ${payment['total']} on booking #{booking.pk} ({booking.customer_name})",
        receipt=receipt,
        s=receipt.snapshot,
    )


BUILDERS = {
    "booking_received": build_booking_received,
    "booking_confirmed": build_booking_confirmed,
    "payment_receipt": build_payment_receipt,
    "refund_receipt": build_refund_receipt,
    "ach_processing": build_ach_processing,
    "owner_new_booking": build_owner_new_booking,
    "owner_payment": build_owner_payment,
}


def render(email):
    """(text, html) bodies of an email."""
    text = render_to_string(f"notifications/email/{email.kind}.txt", email.context)
    html = render_to_string(f"notifications/email/{email.kind}.html", email.context)
    return text, html


def send(kind, object_id):
    """Build and send one email, once. Returns True when it was sent now."""
    builder = BUILDERS[kind]
    email = builder(object_id)
    if email is None or SentEmail.objects.filter(key=email.key).exists():
        return False
    text, html = render(email)
    message = EmailMultiAlternatives(
        subject=email.subject,
        body=text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=email.to,
        reply_to=[settings.BUSINESS_EMAIL],
    )
    message.attach_alternative(html, "text/html")
    for attachment in email.attachments:
        message.attach(*attachment)
    with transaction.atomic():
        try:
            with transaction.atomic():
                SentEmail.objects.create(
                    key=email.key,
                    kind=email.kind,
                    booking=email.booking,
                    to=", ".join(email.to),
                    subject=email.subject,
                    sent_at=timezone.now(),
                )
        except IntegrityError:
            return False
        message.send()
    return True
