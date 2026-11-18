"""Read-only logs: emails sent, every notification on every channel, and SMS opt-outs."""

from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from unfold.admin import ModelAdmin

from apps.notifications.models import Notification, SentEmail, SmsOptOut


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


class ReadOnlyAdmin(ModelAdmin):
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


NOTIFICATION_FIELDS = [
    "created_at",
    "channel",
    "kind",
    "recipient",
    "status",
    "send_after",
    "sent_at",
    "provider_id",
    "error",
]


@admin.register(Notification)
class NotificationAdmin(ReadOnlyAdmin):
    list_display = ["created_at", "channel", "kind", "recipient", "status", "booking"]
    list_filter = ["channel", "status", "kind"]
    search_fields = ["unique_key", "recipient", "provider_id", "booking__id"]
    readonly_fields = [*NOTIFICATION_FIELDS, "booking", "unique_key", "body", "attempts"]


@admin.register(SmsOptOut)
class SmsOptOutAdmin(ReadOnlyAdmin):
    list_display = ["phone", "opted_out", "opted_out_at", "opted_in_at"]
    list_filter = ["opted_out"]
    search_fields = ["phone"]
    readonly_fields = ["phone", "opted_out", "opted_out_at", "opted_in_at"]


def notifications_table(booking):
    """Every message sent or skipped for a booking, newest first, as a read-only table."""
    if booking is None or booking.pk is None:
        return "-"
    rows = [
        format_html(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
            timezone.localtime(note.created_at).strftime("%b %d, %Y %H:%M"),
            note.get_channel_display(),
            note.kind,
            note.recipient,
            note.get_status_display(),
            timezone.localtime(note.send_after).strftime("%b %d %H:%M") if note.send_after else "",
            note.error or note.provider_id,
        )
        for note in booking.notifications.order_by("-created_at", "-pk")
    ]
    if not rows:
        return "Nothing sent yet."
    header = format_html(
        "<tr>{}</tr>",
        format_html_join(
            "",
            "<th>{}</th>",
            ((h,) for h in ("When", "Channel", "Kind", "To", "Status", "Send after", "Detail")),
        ),
    )
    return format_html(
        "<table>{}{}</table>", header, format_html_join("", "{}", ((r,) for r in rows))
    )
