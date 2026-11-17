"""Read-only admin for issued invoices and receipts, with PDF downloads."""

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join
from unfold.admin import ModelAdmin

from apps.invoices.models import Invoice
from apps.invoices.services import pdf


def pdf_link(invoice):
    url = reverse("admin:invoices_invoice_pdf", args=[invoice.pk])
    return format_html('<a href="{}">Download PDF</a>', url)


class ReadOnlyMixin:
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Invoice)
class InvoiceAdmin(ReadOnlyMixin, ModelAdmin):
    """Documents are issued by the services only and never changed."""

    list_display = ["number", "kind", "booking", "issued_at", "total", "balance_due", "download"]
    list_filter = ["kind", "issued_at"]
    list_select_related = ["booking"]
    search_fields = ["number", "booking__customer_name", "booking__email", "booking__id"]
    readonly_fields = [
        "number",
        "kind",
        "booking",
        "issued_at",
        "payment",
        "supersedes",
        "note",
        "issued_by",
        "snapshot",
        "download",
    ]

    @admin.display(description="Total")
    def total(self, invoice):
        return invoice.snapshot.get("total")

    @admin.display(description="Balance due")
    def balance_due(self, invoice):
        return invoice.snapshot.get("balance_due")

    @admin.display(description="PDF")
    def download(self, invoice):
        return pdf_link(invoice)

    def get_urls(self):
        pdf_url = path(
            "<int:invoice_id>/pdf/",
            self.admin_site.admin_view(self.pdf_view),
            name="invoices_invoice_pdf",
        )
        return [pdf_url, *super().get_urls()]

    def pdf_view(self, request, invoice_id):
        """The document's PDF, rendered from its snapshot, for staff who can view invoices."""
        if not self.has_view_permission(request):
            raise PermissionDenied
        invoice = get_object_or_404(Invoice, pk=invoice_id)
        response = HttpResponse(pdf.render_pdf(invoice), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{invoice.filename}"'
        return response


def documents_table(booking):
    """A booking's invoices and receipts as a small read-only table with PDF links."""
    if booking is None or booking.pk is None:
        return "-"
    rows = [
        format_html(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
            invoice.number,
            invoice.get_kind_display(),
            invoice.issued_at.strftime("%b %d, %Y %H:%M"),
            invoice.supersedes.number if invoice.supersedes_id else "",
            pdf_link(invoice),
        )
        for invoice in booking.invoices.select_related("supersedes").order_by("-issued_at", "-pk")
    ]
    if not rows:
        return "None issued yet."
    header = format_html(
        "<tr><th>{}</th><th>{}</th><th>{}</th><th>{}</th><th>{}</th></tr>",
        "Number",
        "Kind",
        "Issued",
        "Replaces",
        "PDF",
    )
    return format_html(
        "<table>{}{}</table>", header, format_html_join("", "{}", ((r,) for r in rows))
    )
