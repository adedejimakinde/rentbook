"""Back-office screens for site content."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.core.models import Review


@admin.register(Review)
class ReviewAdmin(ModelAdmin):
    """Staff enter real reviews here and choose which ones the website shows."""

    list_display = ("customer_name", "city", "rating", "source", "review_date", "is_published")
    list_filter = ("is_published", "source", "rating")
    list_editable = ("is_published",)
    search_fields = ("customer_name", "city", "text")
    date_hierarchy = "review_date"
    fields = ("customer_name", "city", "rating", "source", "review_date", "text", "is_published")
