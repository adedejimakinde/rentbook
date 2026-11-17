"""Invoice numbers: ATG-<year>-0001 upwards, one sequence for all kinds, never a gap."""

from datetime import UTC, datetime
from unittest import mock

import pytest
from django.db import transaction
from django.utils import timezone

from apps.bookings.tests.helpers import booking_worth
from apps.invoices.models import Invoice, InvoiceSequence
from apps.invoices.services import issuing, numbering
from apps.payments.tests.helpers import run_together

pytestmark = pytest.mark.django_db


def test_numbers_count_up_across_kinds_within_the_year():
    year = timezone.localdate().year
    booking = booking_worth("100.00", "quoted")

    first, _ = issuing.issue_booking_invoice(booking.pk)
    second = issuing.issue_manual_invoice(booking, note="Agreed by phone")

    assert first.number == f"ATG-{year}-0001"
    assert second.number == f"ATG-{year}-0002"


def test_a_new_year_starts_again_at_one():
    booking = booking_worth("100.00", "quoted")
    december = timezone.make_aware(datetime(2026, 12, 31, 23, 59))
    january = timezone.make_aware(datetime(2027, 1, 1, 0, 1))

    with mock.patch("apps.invoices.services.issuing.timezone.now", return_value=december):
        old = issuing.issue_manual_invoice(booking)
    with mock.patch("apps.invoices.services.issuing.timezone.now", return_value=january):
        new = issuing.issue_manual_invoice(booking)

    assert old.number == "ATG-2026-0001"
    assert new.number == "ATG-2027-0001"


def test_the_year_is_taken_in_the_business_time_zone(settings):
    settings.TIME_ZONE = "America/Chicago"
    booking = booking_worth("100.00", "quoted")
    # 03:00 UTC on Jan 1 is still Dec 31 in Chicago.
    instant = datetime(2027, 1, 1, 3, 0, tzinfo=UTC)

    with mock.patch("apps.invoices.services.issuing.timezone.now", return_value=instant):
        invoice = issuing.issue_manual_invoice(booking)

    assert invoice.number == "ATG-2026-0001"


def test_a_rolled_back_issue_gives_its_number_back():
    year = timezone.localdate().year
    booking = booking_worth("100.00", "quoted")

    with pytest.raises(RuntimeError), transaction.atomic():
        issuing.issue_manual_invoice(booking)
        raise RuntimeError("something later in the same transaction failed")

    assert not Invoice.objects.exists()
    assert issuing.issue_manual_invoice(booking).number == f"ATG-{year}-0001"


@pytest.mark.django_db(transaction=True)
def test_numbers_are_only_taken_inside_a_transaction():
    with pytest.raises(RuntimeError, match="inside transaction.atomic"):
        numbering.next_number(2026)


@pytest.mark.django_db(transaction=True)
def test_documents_issued_at_once_get_consecutive_numbers_with_no_gap_or_repeat():
    """Eight bookings issue at the same instant, in separate connections."""
    year = timezone.localdate().year
    bookings = [booking_worth("100.00", "quoted") for _ in range(8)]

    results = run_together(
        *[lambda booking=booking: issuing.issue_manual_invoice(booking) for booking in bookings]
    )

    assert not [result for result in results if isinstance(result, Exception)]
    numbers = sorted(Invoice.objects.values_list("number", flat=True))
    assert numbers == [f"ATG-{year}-{n:04d}" for n in range(1, 9)]
    assert InvoiceSequence.objects.get(year=year).last_number == 8


@pytest.mark.django_db(transaction=True)
def test_the_first_documents_of_a_year_issued_at_once_share_one_sequence():
    """No sequence row exists yet; both creators race to make it."""
    year = timezone.localdate().year
    first, second = booking_worth("100.00", "quoted"), booking_worth("100.00", "quoted")

    run_together(
        lambda: issuing.issue_manual_invoice(first),
        lambda: issuing.issue_manual_invoice(second),
    )

    assert sorted(Invoice.objects.values_list("number", flat=True)) == [
        f"ATG-{year}-0001",
        f"ATG-{year}-0002",
    ]
    assert InvoiceSequence.objects.filter(year=year).count() == 1
