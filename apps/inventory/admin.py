"""Admin for inventory."""

from django.contrib import admin, messages
from unfold.admin import ModelAdmin

from apps.inventory import services
from apps.inventory.models import StockAdjustment


@admin.register(StockAdjustment)
class StockAdjustmentAdmin(ModelAdmin):
    list_display = ["variant", "reason", "quantity", "resolved_at", "created_at"]
    list_filter = ["reason", ("resolved_at", admin.EmptyFieldListFilter)]
    list_select_related = ["variant__item"]
    search_fields = ["variant__name", "variant__item__name", "note"]
    autocomplete_fields = ["variant"]

    def save_model(self, request, obj, form, change):
        """Save under the variant lock and warn about bookings left short."""
        warning = services.shortage_warning(services.save_adjustment(obj))
        if warning:
            self.message_user(request, warning, messages.WARNING)
