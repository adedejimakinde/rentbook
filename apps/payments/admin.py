"""Admin for payments: a read-only ledger, plus booking actions that write to it."""

import stripe
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from unfold.admin import ModelAdmin

from apps.payments.forms import (
    CautionRefundForm,
    DamageChargeForm,
    ManualPaymentForm,
    RentalRefundForm,
    ResolveMismatchForm,
)
from apps.payments.models import Payment, UnmatchedStripeEvent
from apps.payments.services import stripe as stripe_service

REFUND_PERMISSION = "payments.can_issue_refunds"
TAX_EXEMPTION_PERMISSION = "bookings.can_set_tax_exemption"
# Ledger forms that need a permission beyond changing the booking. Those that
# send money back to a customer need REFUND_PERMISSION.
FORM_PERMISSIONS = {
    "caution-refund": REFUND_PERMISSION,
    "rental-refund": REFUND_PERMISSION,
    "resolve-mismatch": REFUND_PERMISSION,
    "tax-exemption": TAX_EXEMPTION_PERMISSION,
}

LEDGER_FORMS = {
    "manual-payment": ManualPaymentForm,
    "caution-refund": CautionRefundForm,
    "damage-charge": DamageChargeForm,
    "rental-refund": RentalRefundForm,
    "resolve-mismatch": ResolveMismatchForm,
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
        "refund_of",
        "reference",
        "received_at",
    ]
    list_filter = ["status", "kind", "method", "received_at"]
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


@admin.register(UnmatchedStripeEvent)
class UnmatchedStripeEventAdmin(ModelAdmin):
    """Stripe events no payment matched yet. Retried by the periodic sync; read-only here."""

    list_display = ["event_id", "type", "object_id", "attempts", "resolved_at", "created_at"]
    list_filter = ["type", ("resolved_at", admin.EmptyFieldListFilter)]
    search_fields = ["event_id", "object_id"]
    readonly_fields = [
        "event_id",
        "type",
        "object_id",
        "payload",
        "attempts",
        "resolved_at",
        "created_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class BookingPaymentActionsMixin:
    """Booking admin actions to record manual payments and caution outcomes."""

    payment_actions = [
        "record_manual_payment",
        "record_caution_refund",
        "record_damage_charge",
        "refund_rental_payment",
        "resolve_mismatched_payment",
        "sync_with_stripe",
    ]
    money_fields = [
        "total",
        "deposit_amount",
        "amount_paid",
        "balance_due",
        "overpaid_refundable",
        "caution_held",
        "caution_to_collect",
        "caution_to_refund",
    ]
    ledger_forms = LEDGER_FORMS

    @admin.display(description="Overpaid (refundable)")
    def overpaid_refundable(self, booking):
        return booking.overpaid

    @admin.display(description="Caution to collect")
    def caution_to_collect(self, booking):
        return booking.caution_due

    @admin.display(description="Caution to refund")
    def caution_to_refund(self, booking):
        return booking.caution_excess

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

    def has_issue_refunds_permission(self, request):
        """Refunds send money back, so they need their own permission."""
        return request.user.has_perm(REFUND_PERMISSION)

    @admin.action(description="Record caution deposit refund", permissions=["issue_refunds"])
    def record_caution_refund(self, request, queryset):
        return self._open_ledger_form(request, queryset, "caution-refund")

    @admin.action(description="Record damage charge against caution deposit")
    def record_damage_charge(self, request, queryset):
        return self._open_ledger_form(request, queryset, "damage-charge")

    @admin.action(description="Refund rental payment", permissions=["issue_refunds"])
    def refund_rental_payment(self, request, queryset):
        return self._open_ledger_form(request, queryset, "rental-refund")

    @admin.action(
        description="Resolve mismatched payment (Stripe charged a different amount)",
        permissions=["issue_refunds"],
    )
    def resolve_mismatched_payment(self, request, queryset):
        return self._open_ledger_form(request, queryset, "resolve-mismatch")

    @admin.action(description="Sync with Stripe", permissions=["change"])
    def sync_with_stripe(self, request, queryset):
        """Settle the selected bookings' pending payments and refunds from Stripe."""
        synced, failed = 0, []
        for booking in queryset.order_by("pk"):
            try:
                stripe_service.sync_booking(booking)
            except stripe.StripeError as error:
                failed.append(f"Booking #{booking.pk}: {error.user_message or error}")
            else:
                synced += 1
        if synced:
            noun = "booking" if synced == 1 else "bookings"
            self.message_user(request, f"Synced {synced} {noun} with Stripe.", messages.SUCCESS)
        for problem in failed:
            self.message_user(request, f"{problem} (could not be synced).", messages.ERROR)

    def ledger_form_view(self, request, booking_id, action):
        """Show and process one of the ledger forms for a booking."""
        booking = get_object_or_404(self.model, pk=booking_id)
        form_class = self.ledger_forms.get(action)
        if form_class is None or not self.has_change_permission(request, booking):
            raise PermissionDenied
        permission = FORM_PERMISSIONS.get(action)
        if permission and not request.user.has_perm(permission):
            raise PermissionDenied
        data = request.POST if request.method == "POST" else None
        form = form_class(data, booking=booking)
        if request.method == "POST" and form.is_valid() and form.save(request.user):
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
