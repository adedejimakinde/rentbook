"""Admin for bookings. Every save goes through the locked availability check."""

from collections import defaultdict

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms import BaseInlineFormSet
from unfold.admin import ModelAdmin, TabularInline

from apps.bookings.models import Booking, BookingItem
from apps.bookings.services import availability


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


class BookingItemInline(TabularInline):
    model = BookingItem
    formset = BookingItemFormSet
    fields = ["variant", "quantity", "unit_price"]
    autocomplete_fields = ["variant"]
    extra = 0


@admin.register(Booking)
class BookingAdmin(ModelAdmin):
    list_display = ["id", "customer_name", "event_date", "status", "hold_expires_at"]
    list_filter = ["status", "event_date"]
    search_fields = ["customer_name", "email", "phone"]
    date_hierarchy = "event_date"
    inlines = [BookingItemInline]
