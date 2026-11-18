"""Booking list filters: payment state, caution deposit and payment method.

They read the money annotations from apps.dashboard.services.money, which the
booking admin adds to its queryset.
"""

from django.db.models import Q
from unfold.contrib.filters.admin import DropdownFilter

from apps.bookings.models import Booking, PaymentChoice
from apps.payments.models import Payment


class CommaSeparatedStatusFilter(DropdownFilter):
    """Status, one at a time from the list, or several in the URL (status=paid,deposit_paid)."""

    title = "status"
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return Booking.Status.choices

    def queryset(self, request, queryset):
        value = self.value()
        if not value:
            return queryset
        wanted = [part for part in value.split(",") if part in Booking.Status.values]
        return queryset.filter(status__in=wanted)


class PaymentStateFilter(DropdownFilter):
    title = "payment state"
    parameter_name = "payment_state"

    def lookups(self, request, model_admin):
        return [
            ("unpaid", "Nothing paid yet"),
            ("part_paid", "Part paid (balance left)"),
            ("balance_due", "Any balance due"),
            ("paid", "Paid in full"),
        ]

    def queryset(self, request, queryset):
        value = self.value()
        if value == "unpaid":
            return queryset.filter(m_paid=0, m_total__gt=0)
        if value == "part_paid":
            return queryset.filter(m_paid__gt=0, m_balance__gt=0)
        if value == "balance_due":
            return queryset.filter(m_balance__gt=0)
        if value == "paid":
            return queryset.filter(m_paid__gt=0, m_balance=0)
        return queryset


class CautionFilter(DropdownFilter):
    title = "caution deposit"
    parameter_name = "caution"

    def lookups(self, request, model_admin):
        return [("held", "Held now"), ("none", "None held")]

    def queryset(self, request, queryset):
        if self.value() == "held":
            return queryset.filter(m_caution_held__gt=0)
        if self.value() == "none":
            return queryset.filter(m_caution_held__lte=0)
        return queryset


class PaymentMethodFilter(DropdownFilter):
    """Bookings paid by a method, or that said they would pay that way when booking online."""

    title = "payment method"
    parameter_name = "payment_method"

    def lookups(self, request, model_admin):
        return Payment.Method.choices

    def queryset(self, request, queryset):
        method = self.value()
        if method not in Payment.Method.values:
            return queryset
        paid_this_way = Payment.objects.filter(method=method).values("booking_id")
        match = Q(pk__in=paid_this_way)
        if method in PaymentChoice.values:
            match |= Q(payment_choice=method)
        return queryset.filter(match)
