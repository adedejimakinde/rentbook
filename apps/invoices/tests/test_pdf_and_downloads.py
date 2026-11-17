"""PDFs from the snapshot, immutability, the manual invoice action, and who can download what."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking, PaymentChoice
from apps.bookings.services import links
from apps.bookings.tests.helpers import booking_worth
from apps.bookings.tests.test_admin import booking_form
from apps.invoices.models import Invoice, InvoiceImmutable
from apps.invoices.services import issuing
from apps.invoices.tests.helpers import documents, online_booking, pdf_text
from apps.payments.models import Payment
from apps.payments.services import ledger

pytestmark = pytest.mark.django_db
D = Decimal


@pytest.fixture
def committed(run_tasks, django_capture_on_commit_callbacks):
    def block():
        return django_capture_on_commit_callbacks(execute=True)

    return block


@pytest.fixture
def paid_deposit(committed, event_date):
    """An online Zelle booking with a $10 caution per chair and its deposit paid by Zelle."""
    with committed():
        booking = online_booking(event_date, caution="10.00", choice=PaymentChoice.ZELLE)
    with committed():
        ledger.record_manual_payment(
            booking,
            method=Payment.Method.ZELLE,
            amount=D("80.00"),
            caution_amount=D("40.00"),
            received_at=timezone.now(),
            reference="ZL-555",
            received_by=None,
        )
    return booking


def status_token(booking):
    return links.make_token(booking, links.STATUS)


def test_the_invoice_pdf_shows_the_key_amounts(paid_deposit):
    invoice = documents(paid_deposit, Invoice.Kind.INVOICE)[0]

    text = pdf_text(invoice)

    assert invoice.number in text
    assert "Ada Planner" in text
    assert "Gold Chiavari Chair (Default)" in text
    for amount in ("$25.00", "$100.00", "$40.00", "$20.00", "$160.00"):
        assert amount in text
    assert "Refundable caution deposit" in text
    assert "$40.00" in text
    assert "not part of the rental total" in text
    assert "Balance due: $160.00" in text
    assert "/payments/balance/" in text
    assert "Booking 1" in text or f"Booking {paid_deposit.pk}" in text
    assert "pay@rentals.example" in text
    assert "$AtglaceEvents" in text
    assert "111000025" in text


def test_the_receipt_pdf_shows_the_payment_paid_and_balance(paid_deposit):
    receipt = documents(paid_deposit, Invoice.Kind.RECEIPT)[0]

    text = pdf_text(receipt)

    assert "Receipt" in text
    assert "Payment received" in text
    assert "$120.00" in text  # rental $80 + caution $40
    assert "ZL-555" in text
    assert "Zelle" in text
    assert "Balance due: $80.00" in text


def test_a_paid_in_full_receipt_says_so_and_has_no_payment_instructions(committed, event_date):
    with committed():
        booking = online_booking(event_date, choice=PaymentChoice.ZELLE)
        ledger.record_manual_payment(
            booking,
            method=Payment.Method.CASHAPP,
            amount=D("160.00"),
            received_at=timezone.now(),
            reference="CA-1",
            received_by=None,
        )
    receipt = documents(booking, Invoice.Kind.RECEIPT)[0]

    text = pdf_text(receipt)

    assert "Rental paid in full" in text
    assert "Paying by Zelle" not in text


def test_the_pdf_is_a_letter_size_pdf(paid_deposit):
    from apps.invoices.services import pdf

    invoice = documents(paid_deposit)[0]
    data = pdf.render_pdf(invoice)
    document = pdf.render_document(invoice)

    assert data.startswith(b"%PDF")
    assert (round(document.pages[0].width), round(document.pages[0].height)) == (816, 1056)


def test_the_pdf_is_rendered_from_the_snapshot_not_the_booking(paid_deposit):
    invoice = documents(paid_deposit, Invoice.Kind.INVOICE)[0]
    Booking.objects.filter(pk=paid_deposit.pk).update(
        customer_name="Someone Else", delivery_fee=D("500.00")
    )

    text = pdf_text(invoice)

    assert "Ada Planner" in text
    assert "Someone Else" not in text
    assert "$500.00" not in text


def test_an_issued_document_cannot_be_changed_or_deleted(paid_deposit):
    invoice = documents(paid_deposit)[0]

    invoice.note = "changed"
    with pytest.raises(InvoiceImmutable):
        invoice.save()
    with pytest.raises(InvoiceImmutable):
        invoice.delete()
    with pytest.raises(InvoiceImmutable):
        Invoice.objects.filter(pk=invoice.pk).update(note="changed")
    with pytest.raises(InvoiceImmutable):
        Invoice.objects.all().delete()
    invoice.refresh_from_db()
    assert invoice.note == ""


def test_manual_invoice_action_issues_a_snapshot_with_the_note(admin_client, paid_deposit):
    original = documents(paid_deposit, Invoice.Kind.INVOICE)[0]
    url = reverse("admin:bookings_booking_ledger", args=[paid_deposit.pk, "manual-invoice"])

    assert admin_client.get(url).status_code == 200
    response = admin_client.post(url, {"note": "Price agreed by phone on May 2."})

    assert response.status_code == 302
    manual = Invoice.objects.get(kind=Invoice.Kind.MANUAL_INVOICE)
    assert manual.note == "Price agreed by phone on May 2."
    assert manual.supersedes == original
    assert manual.issued_by.is_superuser
    assert manual.snapshot["amount_paid"] == "80.00"
    assert "Price agreed by phone on May 2." in pdf_text(manual)


def test_manual_invoice_action_is_offered_in_the_booking_admin(admin_client):
    booking = booking_worth("100.00", Booking.Status.QUOTED)

    response = admin_client.post(
        reverse("admin:bookings_booking_changelist"),
        {"action": "issue_manual_invoice", "_selected_action": [booking.pk]},
    )

    assert response.status_code == 302
    assert response["Location"].endswith(f"/{booking.pk}/ledger/manual-invoice/")


def test_booking_admin_lists_its_documents_read_only_with_download_links(
    admin_client, paid_deposit
):
    response = admin_client.get(reverse("admin:bookings_booking_change", args=[paid_deposit.pk]))

    content = response.content.decode()
    for invoice in documents(paid_deposit):
        assert invoice.number in content
        assert reverse("admin:invoices_invoice_pdf", args=[invoice.pk]) in content


def test_an_admin_edit_before_payment_saves_and_issues_a_revised_invoice(
    admin_client, committed, event_date
):
    with committed():
        booking = online_booking(event_date)
    item = booking.items.get()
    data = booking_form(
        Booking.Status.HELD,
        [{"id": item.pk, "variant": item.variant, "quantity": 4, "unit_price": "30.00"}],
        event_date=event_date,
        initial_forms=1,
        hold_expires_at=timezone.localtime(booking.hold_expires_at),
    )

    with committed():
        response = admin_client.post(
            reverse("admin:bookings_booking_change", args=[booking.pk]), data
        )

    assert response.status_code == 302
    original, revised = documents(booking, Invoice.Kind.INVOICE)
    assert original.snapshot["subtotal"] == "100.00"
    assert revised.snapshot["subtotal"] == "120.00"
    assert revised.supersedes == original


def test_staff_download_a_pdf_from_the_admin(admin_client, paid_deposit):
    invoice = documents(paid_deposit)[0]

    response = admin_client.get(reverse("admin:invoices_invoice_pdf", args=[invoice.pk]))

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert invoice.filename in response["Content-Disposition"]


def test_the_admin_pdf_needs_a_staff_login(client, paid_deposit):
    invoice = documents(paid_deposit)[0]

    response = client.get(reverse("admin:invoices_invoice_pdf", args=[invoice.pk]))

    assert response.status_code == 302
    assert "login" in response["Location"]


def test_invoices_are_read_only_in_the_admin(admin_client, paid_deposit):
    invoice = documents(paid_deposit)[0]

    assert admin_client.get(reverse("admin:invoices_invoice_changelist")).status_code == 200
    page = admin_client.get(reverse("admin:invoices_invoice_change", args=[invoice.pk]))
    assert page.status_code == 200
    assert admin_client.get(reverse("admin:invoices_invoice_add")).status_code == 403
    deleted = admin_client.post(
        reverse("admin:invoices_invoice_delete", args=[invoice.pk]), {"post": "yes"}
    )
    assert deleted.status_code == 403
    assert Invoice.objects.filter(pk=invoice.pk).exists()


def test_the_status_page_lists_the_documents_with_download_links(client, paid_deposit):
    token = status_token(paid_deposit)

    response = client.get(reverse("bookings:status", args=[token]))

    content = response.content.decode()
    assert "Invoices and receipts" in content
    for invoice in documents(paid_deposit):
        assert invoice.number in content
        assert reverse("invoices:customer_pdf", args=[token, invoice.number]) in content


def test_the_status_page_has_no_documents_section_before_any_is_issued(client):
    booking = booking_worth("100.00", Booking.Status.QUOTED)

    response = client.get(reverse("bookings:status", args=[status_token(booking)]))

    assert "Invoices and receipts" not in response.content.decode()


def test_a_customer_downloads_their_pdf_by_the_status_link(client, paid_deposit):
    invoice = documents(paid_deposit)[0]

    response = client.get(
        reverse("invoices:customer_pdf", args=[status_token(paid_deposit), invoice.number])
    )

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert "no-cache" in response["Cache-Control"] or "no-store" in response["Cache-Control"]


def test_a_status_link_cannot_download_another_bookings_document(client, paid_deposit):
    other = booking_worth("100.00", Booking.Status.QUOTED)
    issued = issuing.issue_manual_invoice(other)

    response = client.get(
        reverse("invoices:customer_pdf", args=[status_token(paid_deposit), issued.number])
    )

    assert response.status_code == 404


@pytest.mark.parametrize("purpose", [links.BALANCE, links.MANUAL])
def test_a_link_made_for_another_page_cannot_download(client, paid_deposit, purpose):
    invoice = documents(paid_deposit)[0]
    token = links.make_token(paid_deposit, purpose)

    response = client.get(reverse("invoices:customer_pdf", args=[token, invoice.number]))

    assert response.status_code == 404


def test_a_tampered_or_expired_link_cannot_download(client, paid_deposit):
    invoice = documents(paid_deposit)[0]
    token = status_token(paid_deposit)
    expired = links.make_token(paid_deposit, links.STATUS, now=timezone.now() - timedelta(days=400))

    tampered = client.get(reverse("invoices:customer_pdf", args=[token + "x", invoice.number]))
    old = client.get(reverse("invoices:customer_pdf", args=[expired, invoice.number]))

    assert tampered.status_code == 404
    assert old.status_code == 410
