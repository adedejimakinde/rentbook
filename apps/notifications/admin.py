"""Read-only log of emails sent."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.notifications.models import SentEmail


@admin.register(SentEmail)
class SentEmailAdmin(ModelAdmin):
    list_display = ["sent_at", "kind", "to", "subject", "booking"]
    list_filter = ["kind", "sent_at"]
    search_fields = ["to", "subject", "key", "booking__id"]
    readonly_fields = ["key", "kind", "booking", "to", "subject", "sent_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
