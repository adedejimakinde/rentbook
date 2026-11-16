"""Admin for inventory."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.inventory.models import StockAdjustment


@admin.register(StockAdjustment)
class StockAdjustmentAdmin(ModelAdmin):
    list_display = ["variant", "reason", "quantity", "resolved_at", "created_at"]
    list_filter = ["reason", ("resolved_at", admin.EmptyFieldListFilter)]
    list_select_related = ["variant__item"]
    search_fields = ["variant__name", "variant__item__name", "note"]
    autocomplete_fields = ["variant"]
