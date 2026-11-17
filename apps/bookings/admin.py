"""Admin for bookings. Every save goes through the locked availability check.

Once a booking has a payment on it, its status, money fields and lines are
read-only here: they change only through the lifecycle and ledger services,
which check the money first.
"""

from collections import defaultdict

from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.forms import BaseInlineFormSet, ModelForm
from django.shortcuts import redirect
from django.urls import reverse
from unfold.admin import ModelAdmin, TabularInline

from apps.bookings.forms import LIFECYCLE_FORMS
from apps.bookings.models import (
    Booking,
    BookingDeleteRefused,
    BookingItem,
    BookingPackage,
    DeliveryZone,
    PromoCode,
)
from apps.bookings.services import availability, lifecycle, quote
from apps.payments.admin import TAX_EXEMPTION_PERMISSION, BookingPaymentActionsMixin

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


def stock_error(error):
    """The admin's message for an InsufficientStockError."""
    return ValidationError(
        [f"Not enough stock on {error.event_date:%b %d, %Y} to save this booking."]
        + [str(shortage) for shortage in error.shortages]
    )


def check_saved_lines(booking, event_date, status):
    """Lock and check a booking's saved lines for event_date, if status ties up stock."""
    if not booking.pk or not event_date or status not in availability.STOCK_HOLDING_STATUSES:
        return
    quantities = defaultdict(int)
    for variant_id, quantity in booking.items.values_list("variant_id", "quantity"):
        quantities[variant_id] += quantity
    if not quantities:
        return
    try:
        availability.lock_and_check(quantities, event_date, exclude_booking=booking)
    except availability.InsufficientStockError as error:
        raise stock_error(error) from error


class BookingForm(ModelForm):
    """Makes sure a hold placed by staff always has an expiry, and dates are checked.

    lines_editable is set by the admin for each request. When the lines cannot
    be edited (the booking has payments, or the user has no permission on
    booking items, so the inline is read-only or hidden), the saved lines are
    what will be moved, so they are checked here against the new date and
    status. Otherwise the item formset checks the lines as edited.
    """

    lines_editable = True

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
        if not self.lines_editable and not self.errors:
            check_saved_lines(
                self.instance,
                cleaned.get("event_date", self.instance.event_date),
                cleaned.get("status", self.instance.status),
            )
        return cleaned


class BookingItemFormSet(BaseInlineFormSet):
    """Rejects the whole booking form when its edited items would overbook stock.

    The admin change view runs in one transaction, so the variant locks taken
    here are held until the booking and its items have been saved. Read-only
    lines are checked by BookingForm instead.
    """

    def clean(self):
        super().clean()
        booking = self.instance
        if any(self.errors) or not booking.event_date or has_payments(booking):
            return
        if booking.status not in availability.STOCK_HOLDING_STATUSES:
            return

        quantities = defaultdict(int)
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
            raise stock_error(error) from error


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
        "set_tax_exemption",
        "mark_delivered",
        "mark_returned",
        "cancel_booking",
        "reopen_booking",
    ]
    ledger_forms = {**BookingPaymentActionsMixin.ledger_forms, **LIFECYCLE_FORMS}
    search_fields = ["customer_name", "email", "phone"]
    date_hierarchy = "event_date"
    readonly_fields = [
        "tax",
        "caution_fee",
        "promo_code",
        "promo_redeemed_at",
        "needs_attention",
        "delivery_override_note",
        "delivery_override_by",
        "delivery_override_at",
        "clearing_override_note",
        "clearing_override_by",
        "clearing_override_at",
        "tax_exempt",
        "tax_exempt_certificate",
        "tax_exempt_by",
        "tax_exempt_at",
    ]
    inlines = [BookingItemInline, BookingPackageInline]

    def get_form(self, request, obj=None, change=False, **kwargs):
        """Tell the form whether this user can edit the lines, so it knows what to check."""
        form = super().get_form(request, obj, change=change, **kwargs)
        inline = BookingItemInline(self.model, self.admin_site)
        form.lines_editable = obj is None or inline.has_change_permission(request, obj)
        return form

    def delete_view(self, request, object_id, extra_context=None):
        """A delete refused by the booking itself (it took stock or money meanwhile) is an error.

        The admin checks first, but a booking can be put on hold or paid
        between that check and the delete.
        """
        try:
            return super().delete_view(request, object_id, extra_context)
        except BookingDeleteRefused as error:
            self.message_user(request, str(error), messages.ERROR)
            return redirect(reverse("admin:bookings_booking_change", args=[object_id]))

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
        if has_payments(obj):
            fields += [field for field in LOCKED_WITH_PAYMENTS if field not in fields]
        return fields

    def save_related(self, request, form, formsets, change):
        """Once the lines are saved, recompute tax and caution from them (no payments yet)."""
        super().save_related(request, form, formsets, change)
        if not has_payments(form.instance):
            quote.reprice_booking(form.instance)

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

    def has_set_tax_exemption_permission(self, request):
        return request.user.has_perm(TAX_EXEMPTION_PERMISSION)

    @admin.action(description="Set tax exemption", permissions=["set_tax_exemption"])
    def set_tax_exemption(self, request, queryset):
        return self._open_ledger_form(request, queryset, "tax-exemption")

    @admin.action(description="Mark delivered")
    def mark_delivered(self, request, queryset):
        return self._open_ledger_form(request, queryset, "mark-delivered")

    @admin.action(description="Mark returned")
    def mark_returned(self, request, queryset):
        return self._open_ledger_form(request, queryset, "mark-returned")

    @admin.action(description="Cancel booking")
    def cancel_booking(self, request, queryset):
        return self._open_ledger_form(request, queryset, "cancel")

    @admin.action(description="Reopen cancelled booking")
    def reopen_booking(self, request, queryset):
        return self._open_ledger_form(request, queryset, "reopen")


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
