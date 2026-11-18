"""Money totals for many bookings in one query.

These mirror the Booking and Payment properties (total, amount_paid,
balance_due, caution_held, caution_due) as annotations, so lists and the
dashboard never run a query per booking. Annotation names start with m_ because
Booking already has read-only properties called total and balance_due.
"""

from decimal import Decimal

from django.db.models import DecimalField, F, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce, Greatest, Least

from apps.bookings.models import BookingItem, BookingPackage
from apps.payments.models import Payment

MONEY = DecimalField(max_digits=12, decimal_places=2)
ZERO = Value(Decimal("0.00"), output_field=MONEY)


def _sum_of(queryset, expression):
    """A subquery giving one booking's sum of expression, or zero."""
    rows = queryset.filter(booking=OuterRef("pk")).order_by().values("booking")
    summed = rows.annotate(value=Sum(expression, output_field=MONEY)).values("value")[:1]
    return Coalesce(Subquery(summed, output_field=MONEY), ZERO)


def with_money(bookings):
    """Annotate bookings with m_total, m_paid, m_balance, m_caution_held and m_caution_due."""
    received = Payment.objects.filter(
        kind__in=Payment.RENTAL_KINDS,
        status__in=(Payment.Status.SUCCEEDED, Payment.Status.REFUNDED),
    )
    released = Payment.objects.filter(
        kind__in=Payment.CAUTION_RELEASE_KINDS, status=Payment.Status.SUCCEEDED
    )
    lines = (F("quantity") - F("package_quantity")) * F("unit_price")
    return (
        bookings.annotate(
            m_lines=_sum_of(BookingItem.objects, lines),
            m_packages=_sum_of(BookingPackage.objects, F("quantity") * F("unit_price")),
            m_paid=_sum_of(received, F("amount") - Least(F("refunded_amount"), F("amount"))),
            m_caution_in=_sum_of(
                received,
                F("caution_amount") - Greatest(F("refunded_amount") - F("amount"), ZERO),
            ),
            m_caution_out=_sum_of(released, F("amount")),
        )
        .annotate(
            m_total=Greatest(
                F("m_lines")
                + F("m_packages")
                - F("discount")
                + F("delivery_fee")
                + F("setup_fee")
                + F("tax"),
                ZERO,
            ),
            m_caution_held=F("m_caution_in") - F("m_caution_out"),
            m_caution_due=Greatest(F("caution_fee") - F("m_caution_in"), ZERO),
        )
        .annotate(m_balance=Greatest(F("m_total") - F("m_paid"), ZERO))
    )
