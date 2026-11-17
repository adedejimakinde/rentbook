"""Customer PDF downloads, by the same signed link as the booking status page."""

from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.bookings.services import links
from apps.invoices.services import pdf


@never_cache
@require_GET
def customer_pdf(request, token, number):
    """One of the booking's documents as a PDF. 404 for any document of another booking."""
    try:
        booking = links.read_token(token, links.STATUS)
    except links.ExpiredLink:
        return render(request, "bookings/link_invalid.html", {"expired": True}, status=410)
    except links.BadLink:
        return render(request, "bookings/link_invalid.html", {}, status=404)
    invoice = booking.invoices.filter(number=number).first()
    if invoice is None:
        raise Http404("No such document.")
    response = HttpResponse(pdf.render_pdf(invoice), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{invoice.filename}"'
    response["X-Robots-Tag"] = "noindex"
    return response
