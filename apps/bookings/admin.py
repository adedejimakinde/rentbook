"""Admin for bookings. Every save goes through the locked availability check.

Once a booking has a payment on it, its status, money fields and lines are
read-only here: they change only through the lifecycle and ledger services,
which check the money first.
"""

from collections import defaultdict

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms import BaseInlineFormSet, ModelForm
from unfold.admin import ModelAdmin, TabularInline

from apps.bookings.forms import LIFECYCLE_FORMS
from apps.bookings.models import Booking, BookingItem, BookingPackage, DeliveryZone, PromoCode
from apps.bookings.services import availability, lifecycle
from apps.payments.admin import BookingPaymentActionsMixin

# Statuses staff may set by hand. Every other status is reached through a
# payment or a lifecycle action, never by editing the booking.
HAND_SET_STATUSES = (Booking.Status.ENQUIRY, Booking.Status.QUOTED, Booking.Status.HELD)
LOCKED_WITH_PAYMENTS = [
    "status",
    "hold_expires_at",
    "delivery_fee",
    "setup_fee",
    "discount",
    "tax",
    "caution_fee",
]


def has_payments(booking):
    """True when any payment entry that moved or may move money is on the booking."""
    return booking is not None and booking.pk and booking.payments.counted().exists()


class BookingForm(ModelForm):
    """Makes sure a hold placed by staff always has an expiry."""

    def clean(self):
        cleaned = super().clean()
        status = cleaned.get("status")
        if status and status != self.instance.status and status not in HAND_SET_STATUSES:
            self.add_error(
                "status",
                "This status cannot be set by hand. It follows from payments, or from "
                "the cancel, delivered and returned actions.",
            )
        if cleaned.get("status") == Booking.Status.HELD and not cleaned.get("hold_expires_at"):
            cleaned["hold_expires_at"] = availability.staff_hold_expiry()
        return cleaned


class BookingItemFormSet(BaseInlineFormSet):
    """Rejects the whole booking form when its items would overbook stock.

    The admin change view runs in one transaction, so the variant locks taken
    here are held until the booking and its items have been saved.
    """

    def clean(self):
        super().clean()
        booking = self.instance
        if any(self.errors) or not booking.event_date:
            return
        if booking.status not in availability.STOCK_HOLDING_STATUSES:
            return

        quantities = defaultdict(int)
        if has_payments(booking):
            # The lines are read-only, so the saved ones are what is being moved.
            for variant_id, quantity in booking.items.values_list("variant_id", "quantity"):
                quantities[variant_id] += quantity
        else:
            for form in self.forms:
                data = getattr(form, "cleaned_data", None)
                if not data or data.get("DELETE") or not data.get("variant"):
                    continue
                quantities[data["variant"].pk] += data["quantity"]
        if not quantities:
            return

        try:
            availability.lock_and_check(quantities, booking.event_date, exclude_booking=booking)
        except availability.InsufficientStockError as error:
            raise ValidationError(
                [f"Not enough stock on {error.event_date:%b %d, %Y} to save this booking."]
                + [str(shortage) for shortage in error.shortages]
            ) from error


class LockedWithPaymentsInline(TabularInline):
    """Lines can be added, changed or removed here only until a payment exists."""

    def has_add_permission(self, request, obj=None):
        return not has_payments(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return not has_payments(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return not has_payments(obj) and super().has_delete_permission(request, obj)


class BookingItemInline(LockedWithPaymentsInline):
    model = BookingItem
    formset = BookingItemFormSet
    fields = ["variant", "quantity", "package_quantity", "unit_price"]
    autocomplete_fields = ["variant"]
    extra = 0


class BookingPackageInline(LockedWithPaymentsInline):
    """Packages as priced on the booking; their stock is on the booking items."""

    model = BookingPackage
    fields = ["package", "name", "quantity", "unit_price"]
    autocomplete_fields = ["package"]
    extra = 0


@admin.register(Booking)
class BookingAdmin(BookingPaymentActionsMixin, ModelAdmin):
    form = BookingForm
    list_display = [
        "id",
        "customer_name",
        "event_date",
        "status",
        "needs_attention",
        "hold_expires_at",
    ]
    list_filter = ["status", "needs_attention", "event_date"]
    actions = [
        *BookingPaymentActionsMixin.payment_actions,
        "change_item_line",
        "mark_delivered",
        "mark_returned",
        "cancel_booking",
    ]
    ledger_forms = {**BookingPaymentActionsMixin.ledger_forms, **LIFECYCLE_FORMS}
    search_fields = ["customer_name", "email", "phone"]
    date_hierarchy = "event_date"
    readonly_fields = [
        "promo_code",
        "promo_redeemed_at",
        "needs_attention",
        "delivery_override_note",
        "delivery_override_by",
        "delivery_override_at",
    ]
    inlines = [BookingItemInline, BookingPackageInline]

    def get_object(self, request, object_id, from_field=None):
        """Lock the booking while it is being saved, so no payment lands mid-edit.

        The admin saves inside one transaction; holding the row lock makes the
        payment check behind the read-only fields true until the save commits.
        """
        booking = super().get_object(request, object_id, from_field)
        if booking is not None and request.method == "POST":
            booking = Booking.objects.select_for_update().get(pk=booking.pk)
        return booking

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        return fields + LOCKED_WITH_PAYMENTS if has_payments(obj) else fields

    def get_actions(self, request):
        """Bookings are deleted one at a time, where stock and payments are checked."""
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def has_delete_permission(self, request, obj=None):
        """A booking that holds stock or has any payment entry cannot be deleted."""
        if obj is not None and (lifecycle.holds_stock(obj) or obj.payments.exists()):
            return False
        return super().has_delete_permission(request, obj)

    @admin.action(description="Change an item line (bookings with payments)")
    def change_item_line(self, request, queryset):
        return self._open_ledger_form(request, queryset, "change-item")

    @admin.action(description="Mark delivered")
    def mark_delivered(self, request, queryset):
        return self._open_ledger_form(request, queryset, "mark-delivered")

    @admin.action(description="Mark returned")
    def mark_returned(self, request, queryset):
        return self._open_ledger_form(request, queryset, "mark-returned")

    @admin.action(description="Cancel booking")
    def cancel_booking(self, request, queryset):
        return self._open_ledger_form(request, queryset, "cancel")


@admin.register(DeliveryZone)
class DeliveryZoneAdmin(ModelAdmin):
    list_display = ["name", "zip_count", "delivery_fee", "setup_fee", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name", "zip_codes"]

    @admin.display(description="ZIP codes")
    def zip_count(self, zone):
        return len(zone.zip_codes)


@admin.register(PromoCode)
class PromoCodeAdmin(ModelAdmin):
    list_display = [
        "code",
        "percent_off",
        "amount_off",
        "valid_from",
        "valid_until",
        "min_subtotal",
        "uses_count",
        "max_uses",
        "is_active",
    ]
    list_filter = ["is_active"]
    search_fields = ["code"]
    readonly_fields = ["uses_count"]
