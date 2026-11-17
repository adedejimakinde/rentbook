"""Gap-free invoice numbers: ATG-2026-0001, counting from 1 again each year."""

from django.db import transaction

from apps.invoices.models import InvoiceSequence

PREFIX = "ATG"


def format_number(year, sequence):
    return f"{PREFIX}-{year}-{sequence:04d}"


def next_number(year):
    """Take the next number for year. Must run inside the transaction that saves the invoice.

    The year's row is locked until that transaction ends, so two documents
    issued at once get consecutive numbers, and a transaction that rolls back
    gives its number back: no number is ever skipped or used twice.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("next_number() must run inside transaction.atomic().")
    InvoiceSequence.objects.bulk_create([InvoiceSequence(year=year)], ignore_conflicts=True)
    sequence = InvoiceSequence.objects.select_for_update().get(year=year)
    sequence.last_number += 1
    sequence.save(update_fields=["last_number", "updated_at"])
    return format_number(year, sequence.last_number)
