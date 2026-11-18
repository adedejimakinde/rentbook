"""Back-office screens for site content and the owner's site settings."""

from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from unfold.admin import ModelAdmin

from apps.core import site_settings
from apps.core.models import Review, SiteSettings, SiteSettingsChange


@admin.register(Review)
class ReviewAdmin(ModelAdmin):
    """Staff enter real reviews here and choose which ones the website shows."""

    list_display = ("customer_name", "city", "rating", "source", "review_date", "is_published")
    list_filter = ("is_published", "source", "rating")
    list_editable = ("is_published",)
    search_fields = ("customer_name", "city", "text")
    date_hierarchy = "review_date"
    fields = ("customer_name", "city", "rating", "source", "review_date", "text", "is_published")


TONES = {
    "success": "background:#e3f4e8;color:#1b6e3c;",
    "warning": "background:#fff1de;color:#8a4b00;",
    "danger": "background:#fce8f1;color:#b0154f;",
}


@admin.register(SiteSettings)
class SiteSettingsAdmin(ModelAdmin):
    fieldsets = [
        ("Status", {"fields": ["availability", "change_history"]}),
        ("Booking", {"fields": ["online_booking_enabled", "allow_deposit"]}),
        (
            "Payment methods",
            {
                "fields": [
                    "card_ach_enabled",
                    "zelle_enabled",
                    "cashapp_enabled",
                    "bank_deposit_enabled",
                ]
            },
        ),
        ("Quotes", {"fields": ["promo_codes_enabled", "tax_exempt_requests_enabled"]}),
        ("Messages", {"fields": ["sms_enabled", "whatsapp_enabled", "owner_alerts_enabled"]}),
        (
            "Reminders",
            {
                "fields": [
                    "reminder_payment_deadline",
                    "reminder_balance_due",
                    "reminder_balance_final",
                    "reminder_owner_unpaid",
                    "reminder_pre_event",
                    "reminder_thank_you",
                ]
            },
        ),
    ]
    readonly_fields = ["availability", "change_history"]

    @admin.display(description="What customers can use now")
    def availability(self, row):
        rows = []
        for item in site_settings.all_availability(row):
            if item.active:
                state, tone = "Available", "success"
            elif not item.configured:
                state, tone = (
                    f"Unavailable: not configured (set {', '.join(item.missing)})",
                    "danger",
                )
            else:
                state, tone = "Off: switched off below", "warning"
            rows.append((item.label, TONES[tone], state))
        return format_html(
            '<ul style="list-style:none;margin:0;padding:0;display:grid;gap:8px">{}</ul>',
            format_html_join(
                "",
                '<li style="display:flex;flex-wrap:wrap;align-items:center;gap:8px">'
                "<strong>{}</strong>"
                '<span style="{}border-radius:999px;padding:3px 10px;font-size:12px;'
                'font-weight:700">{}</span></li>',
                rows,
            ),
        )

    @admin.display(description="Change history")
    def change_history(self, row):
        changes = row.changes.select_related("changed_by")[:20] if row.pk else []
        items = [
            (
                timezone.localtime(change.created_at).strftime("%b %d, %Y %H:%M"),
                change.changed_by or "unknown",
                ", ".join(
                    f"{name}: {'on' if new else 'off'}" for name, (_, new) in change.changes.items()
                ),
            )
            for change in changes
        ]
        if not items:
            return "No changes yet."
        return format_html(
            '<ul style="list-style:none;margin:0;padding:0;display:grid;gap:6px">{}</ul>',
            format_html_join("", "<li>{} by <strong>{}</strong>: {}</li>", items),
        )

    def has_add_permission(self, request):
        return not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        """There is one row: open it."""
        row = site_settings.get()
        return redirect(reverse("admin:core_sitesettings_change", args=[row.pk]))

    def save_model(self, request, obj, form, change):
        """Save, and record which switches changed, from what to what, and by whom."""
        changes = {
            name: [form.initial.get(name), form.cleaned_data.get(name)]
            for name in form.changed_data
        }
        super().save_model(request, obj, form, change)
        if changes:
            SiteSettingsChange.objects.create(
                settings=obj, changed_by=request.user, changes=changes
            )
