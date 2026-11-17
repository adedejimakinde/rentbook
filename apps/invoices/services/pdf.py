"""Render an issued document as a PDF with WeasyPrint, from its snapshot only."""

import base64
import io
from datetime import date, datetime, time
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.bookings.services import links

LOGO = "brand/atglace-logo-full.png"
LOGO_WIDTH_PX = 480
FONT = "fonts/montserrat-latin-500-normal.woff2"
FONT_BOLD = "fonts/montserrat-latin-700-normal.woff2"


def _static_path(name):
    """The file behind a static name, from the source folders or STATIC_ROOT."""
    found = finders.find(name)
    if found:
        return Path(found)
    path = Path(settings.STATIC_ROOT) / name
    return path if path.exists() else None


@lru_cache(maxsize=1)
def logo_data_uri():
    """The full logo shrunk to print size, as a data URI, so each PDF stays small."""
    from PIL import Image

    path = _static_path(LOGO)
    if path is None:
        return ""
    with Image.open(path) as image:
        image = image.convert("RGBA")
        height = round(image.height * LOGO_WIDTH_PX / image.width)
        image = image.resize((LOGO_WIDTH_PX, height), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _font_url(name):
    path = _static_path(name)
    return path.as_uri() if path else ""


def _parse_datetime(value):
    if not value:
        return None
    return timezone.localtime(datetime.fromisoformat(value))


def links_for(booking_id):
    """Absolute status and balance links for a booking, made now (they carry an expiry)."""
    from apps.bookings.models import Booking

    booking = Booking.objects.get(pk=booking_id)
    status = reverse("bookings:status", args=[links.make_token(booking, links.STATUS)])
    balance = reverse("payments:balance", args=[links.make_token(booking, links.BALANCE)])
    return {
        "status_url": f"{settings.SITE_URL}{status}",
        "balance_url": f"{settings.SITE_URL}{balance}",
    }


def document_context(invoice):
    """What the PDF template needs: the snapshot, with its dates parsed for display."""
    data = invoice.snapshot
    event = dict(data["event"])
    event["date"] = date.fromisoformat(event["date"])
    if event.get("start_time"):
        event["start_time"] = time.fromisoformat(event["start_time"])
    payment = dict(data["payment"]) if data.get("payment") else None
    if payment:
        payment["received_at"] = _parse_datetime(payment["received_at"])
    refund = dict(data["refund"]) if data.get("refund") else None
    if refund:
        refund["refunded_at"] = _parse_datetime(refund["refunded_at"])
    booking = dict(data["booking"])
    booking["hold_expires_at"] = _parse_datetime(booking.get("hold_expires_at"))
    return {
        "invoice": invoice,
        "title": invoice.get_kind_display(),
        "is_invoice": invoice.kind in invoice.INVOICE_KINDS,
        "issued_at": timezone.localtime(invoice.issued_at),
        "s": data,
        "event": event,
        "booking": booking,
        "payment": payment,
        "refund": refund,
        "logo": logo_data_uri(),
        "font_regular": _font_url(FONT),
        "font_bold": _font_url(FONT_BOLD),
        **links_for(invoice.booking_id),
    }


def render_document(invoice):
    """The laid-out WeasyPrint document, before it is written as PDF."""
    from weasyprint import HTML

    html = render_to_string("invoices/document.html", document_context(invoice))
    return HTML(string=html, base_url=str(settings.BASE_DIR)).render()


def render_pdf(invoice):
    """The document as PDF bytes."""
    return render_document(invoice).write_pdf()
