"""Admin for payments: a read-only ledger, plus booking actions that write to it."""

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from unfold.admin import ModelAdmin

from apps.payments.forms import CautionRefundForm, DamageChargeForm, ManualPaymentForm
from apps.payments.models import Payment

LEDGER_FORMS = {
    "manual-payment": ManualPaymentForm,
    "caution-refund": CautionRefundForm,
    "damage-charge": DamageChargeForm,
}


@admin.register(Payment)
class PaymentAdmin(ModelAdmin):
    """Payments are written by the ledger only, so the admin can look but not edit."""

    list_display = [
        "id",
        "booking",
        "kind",
        "method",
        "status",
        "amount",
        "caution_amount",
        "refunded_amount",
        "reference",
        "received_at",
    ]
    list_filter = ["status", "kind", "method"]
    list_select_related = ["booking"]
    search_fields = [
        "reference",
        "stripe_payment_intent_id",
        "stripe_checkout_session_id",
        "booking__customer_name",
        "booking__email",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class BookingPaymentActionsMixin:
    """Booking admin actions to record manual payments and caution outcomes."""

    payment_actions = ["record_manual_payment", "record_caution_refund", "record_damage_charge"]
    money_fields = ["total", "deposit_amount", "amount_paid", "balance_due", "caution_held"]

    def get_readonly_fields(self, request, obj=None):
        """Show the computed money fields once the booking exists."""
        fields = list(super().get_readonly_fields(request, obj))
        return fields + self.money_fields if obj else fields

    def get_urls(self):
        ledger_url = path(
            "<int:booking_id>/ledger/<slug:action>/",
            self.admin_site.admin_view(self.ledger_form_view),
            name="bookings_booking_ledger",
        )
        return [ledger_url, *super().get_urls()]

    def _open_ledger_form(self, request, queryset, action):
        if queryset.count() != 1:
            self.message_user(request, "Select exactly one booking.", messages.ERROR)
            return None
        return redirect(reverse("admin:bookings_booking_ledger", args=[queryset.get().pk, action]))

    @admin.action(description="Record manual payment (Zelle, Cash App, bank deposit)")
    def record_manual_payment(self, request, queryset):
        return self._open_ledger_form(request, queryset, "manual-payment")

    @admin.action(description="Record caution deposit refund")
    def record_caution_refund(self, request, queryset):
        return self._open_ledger_form(request, queryset, "caution-refund")

    @admin.action(description="Record damage charge against caution deposit")
    def record_damage_charge(self, request, queryset):
        return self._open_ledger_form(request, queryset, "damage-charge")

    def ledger_form_view(self, request, booking_id, action):
        """Show and process one of the ledger forms for a booking."""
        booking = get_object_or_404(self.model, pk=booking_id)
        form_class = LEDGER_FORMS.get(action)
        if form_class is None or not self.has_change_permission(request, booking):
            raise PermissionDenied
        form = form_class(request.POST or None)
        if request.method == "POST" and form.is_valid() and form.save(booking, request.user):
            self.message_user(request, f"{form.title}: saved.", messages.SUCCESS)
            return redirect(reverse("admin:bookings_booking_change", args=[booking.pk]))
        context = {
            **self.admin_site.each_context(request),
            "title": f"{form.title}: {booking}",
            "form": form,
            "booking": booking,
            "opts": self.model._meta,
        }
        return render(request, "admin/payments/ledger_form.html", context)
