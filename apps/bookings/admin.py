"""Admin for bookings. Every save goes through the locked availability check.

Once a booking has a payment on it, its status, money fields and lines are
read-only here: they change only through the lifecycle and ledger services,
which check the money first.
"""

import re
from collections import defaultdict

from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db.models import Q, Value
from django.db.models.functions import Replace
from django.forms import BaseInlineFormSet, ModelForm
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from unfold.admin import ModelAdmin, TabularInline
from unfold.contrib.filters.admin import BooleanRadioFilter, RangeDateFilter
from unfold.decorators import action

from apps.bookings.admin_filters import (
    CautionFilter,
    CommaSeparatedStatusFilter,
    PaymentMethodFilter,
    PaymentStateFilter,
)
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
from apps.dashboard.services.money import with_money
from apps.dashboard.services.visibility import visibility_for
from apps.invoices.admin import documents_table
from apps.invoices.forms import ManualInvoiceForm
from apps.notifications.admin import notifications_table
from apps.notifications.services import emails as email_service
from apps.notifications.services import reminders
from apps.payments.admin import TAX_EXEMPTION_PERMISSION, BookingPaymentActionsMixin

# Statuses staff may set by hand. Every other status is reached through a
# payment or a lifecycle action, never by editing the booking.
HAND_SET_STATUSES = (Booking.Status.ENQUIRY, Booking.Status.QUOTED, Booking.Status.HELD)
# Fields only people who may see payments can see (Crew cannot).
MONEY_FIELDS = {
    "delivery_fee",
    "setup_fee",
    "discount",
    "tax",
    "caution_fee",
    "promo_code",
    "promo_redeemed_at",
    "payment_choice",
    "payment_plan",
    "tax_exempt",
    "tax_exempt_certificate",
    "tax_exempt_by",
    "tax_exempt_at",
    "tax_exempt_requested",
    "tax_exempt_org_name",
    "tax_exempt_cert_number",
    "delivery_override_note",
    "delivery_override_by",
    "delivery_override_at",
    "clearing_override_note",
    "clearing_override_by",
    "clearing_override_at",
    "invoices_and_receipts",
    "total",
    "deposit_amount",
    "amount_paid",
    "balance_due",
    "overpaid_refundable",
    "caution_held",
    "caution_to_collect",
    "caution_to_refund",
}
MONEY_COLUMNS = {"total_amount", "paid_amount", "balance_amount"}
SEE_PAYMENTS = "payments.view_payment"
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


class PriceHiddenInline(LockedWithPaymentsInline):
    """Hides unit prices from people who may not see payments."""

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if request.user.has_perm(SEE_PAYMENTS):
            return fields
        return [field for field in fields if field != "unit_price"]


class BookingItemInline(PriceHiddenInline):
    model = BookingItem
    formset = BookingItemFormSet
    fields = ["variant", "quantity", "package_quantity", "unit_price"]
    autocomplete_fields = ["variant"]
    extra = 0


class BookingPackageInline(PriceHiddenInline):
    """Packages as priced on the booking; their stock is on the booking items."""

    model = BookingPackage
    fields = ["package", "name", "quantity", "unit_price"]
    autocomplete_fields = ["package"]
    extra = 0


# The booking page, in sections. Every field of the booking is in exactly one section.
BOOKING_FIELDSETS = [
    ("Customer", {"fields": ["customer_name", "email", "phone"]}),
    (
        "Event",
        {
            "fields": [
                "status",
                "needs_attention",
                "event_date",
                "event_type",
                "event_start_time",
                "setup_by_time",
                "hold_expires_at",
                "booking_log",
            ]
        },
    ),
    (
        "Delivery and crew",
        {
            "fields": [
                "delivery_address",
                "city",
                "zip_code",
                "surface",
                "delivery_time",
                "pickup_date",
                "pickup_time",
                "crew_notes",
                "delivery_override_note",
                "delivery_override_by",
                "delivery_override_at",
            ]
        },
    ),
    (
        "Money and payments",
        {
            "fields": [
                "total",
                "deposit_amount",
                "amount_paid",
                "balance_due",
                "overpaid_refundable",
                "caution_held",
                "caution_to_collect",
                "caution_to_refund",
                "delivery_fee",
                "setup_fee",
                "discount",
                "tax",
                "caution_fee",
                "promo_code",
                "promo_redeemed_at",
                "payment_choice",
                "payment_plan",
                "tax_exempt",
                "tax_exempt_certificate",
                "tax_exempt_by",
                "tax_exempt_at",
                "tax_exempt_requested",
                "tax_exempt_org_name",
                "tax_exempt_cert_number",
                "clearing_override_note",
                "clearing_override_by",
                "clearing_override_at",
                "invoices_and_receipts",
            ]
        },
    ),
    (
        "Notifications",
        {
            "fields": [
                "sms_consent",
                "sms_consent_version",
                "sms_consent_at",
                "terms_version",
                "terms_accepted_at",
                "notification_log",
            ]
        },
    ),
]


@admin.register(Booking)
class BookingAdmin(BookingPaymentActionsMixin, ModelAdmin):
    form = BookingForm
    list_display = [
        "id",
        "customer_name",
        "city",
        "event_date",
        "status",
        "total_amount",
        "paid_amount",
        "balance_amount",
        "needs_attention",
        "hold_expires_at",
    ]
    list_filter = [
        ("event_date", RangeDateFilter),
        CommaSeparatedStatusFilter,
        PaymentStateFilter,
        ("needs_attention", BooleanRadioFilter),
        PaymentMethodFilter,
        CautionFilter,
    ]
    list_filter_submit = True
    list_per_page = 25
    actions = [
        *BookingPaymentActionsMixin.payment_actions,
        "change_item_line",
        "set_tax_exemption",
        "mark_delivered",
        "mark_returned",
        "cancel_booking",
        "reopen_booking",
        "issue_manual_invoice",
        "send_invoice_to_customer",
        "resend_last_reminder",
    ]
    ledger_forms = {
        **BookingPaymentActionsMixin.ledger_forms,
        **LIFECYCLE_FORMS,
        "manual-invoice": ManualInvoiceForm,
    }
    search_fields = ["customer_name", "email", "phone", "city"]
    fieldsets = BOOKING_FIELDSETS
    actions_detail = ["resolve_attention_detail"]
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
        "tax_exempt_requested",
        "tax_exempt_org_name",
        "tax_exempt_cert_number",
        "terms_version",
        "terms_accepted_at",
        "payment_choice",
        "payment_plan",
        "sms_consent",
        "sms_consent_version",
        "sms_consent_at",
        "invoices_and_receipts",
        "notification_log",
        "booking_log",
    ]
    inlines = [BookingItemInline, BookingPackageInline]

    def get_queryset(self, request):
        """Every row carries its total, paid and balance, worked out in the one query."""
        return with_money(super().get_queryset(request))

    @admin.display(description="Total", ordering="m_total")
    def total_amount(self, booking):
        return f"${booking.m_total:,.2f}"

    @admin.display(description="Paid", ordering="m_paid")
    def paid_amount(self, booking):
        return f"${booking.m_paid:,.2f}"

    @admin.display(description="Balance", ordering="m_balance")
    def balance_amount(self, booking):
        return f"${booking.m_balance:,.2f}"

    def get_search_results(self, request, queryset, search_term):
        """Also find a booking by its number (1038 or #1038) or a phone in any format."""
        term = search_term.strip()
        found, may_have_duplicates = super().get_search_results(request, queryset, term)
        extra = Q()
        number = term.lstrip("#")
        if number.isdigit():
            extra |= Q(pk=int(number))
        digits = re.sub(r"\D", "", term)
        if len(digits) >= 3 and re.fullmatch(r"[\d\s()+.-]+", term):
            plain = Replace("phone", Value("-"), Value(""))
            for mark in (" ", "(", ")", ".", "+"):
                plain = Replace(plain, Value(mark), Value(""))
            queryset = queryset.annotate(phone_digits=plain)
            extra |= Q(phone_digits__contains=digits)
        if not extra:
            return found, may_have_duplicates
        return queryset.filter(Q(pk__in=found.values("pk")) | extra), may_have_duplicates

    def _hidden_fields(self, request):
        """Fields this user may not see: money for those without payments, phone per the switch."""
        hidden = set()
        if not request.user.has_perm(SEE_PAYMENTS):
            hidden |= MONEY_FIELDS
        if not request.user.has_perm("bookings.view_bookinglogentry"):
            hidden.add("booking_log")
        if not visibility_for(request.user).show_phone:
            hidden.add("phone")
        return hidden

    def get_list_display(self, request):
        columns = list(super().get_list_display(request))
        if request.user.has_perm(SEE_PAYMENTS):
            return columns
        return [column for column in columns if column not in MONEY_COLUMNS]

    def get_fieldsets(self, request, obj=None):
        """The sections, without fields this user may not see or that do not exist yet."""
        hidden = self._hidden_fields(request)
        editable = {field.name for field in self.model._meta.fields if field.editable}
        shown = editable | set(self.get_readonly_fields(request, obj))
        sections = []
        for title, options in self.fieldsets:
            names = [name for name in options["fields"] if name in shown and name not in hidden]
            if names:
                sections.append((title, {**options, "fields": names}))
        return sections

    def get_exclude(self, request, obj=None):
        model_names = {field.name for field in self.model._meta.fields}
        return [
            *(super().get_exclude(request, obj) or []),
            *(self._hidden_fields(request) & model_names),
        ]

    @admin.display(description="Log")
    def booking_log(self, booking):
        """What staff did to this booking and why, newest first."""
        entries = list(booking.log.select_related("by")) if booking.pk else []
        if not entries:
            return "Nothing logged yet."
        rows = [
            (
                timezone.localtime(entry.created_at).strftime("%b %d, %Y %H:%M"),
                entry.get_action_label(),
                entry.by or "unknown",
                entry.note,
            )
            for entry in entries
        ]
        return format_html(
            '<ul class="grid list-none gap-2 p-0 m-0">{}</ul>',
            format_html_join("", "<li><strong>{}</strong>: {} by {}. {}</li>", rows),
        )

    @admin.display(description="Invoices and receipts")
    def invoices_and_receipts(self, booking):
        return documents_table(booking)

    @admin.display(description="Notification log")
    def notification_log(self, booking):
        return notifications_table(booking)

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
        if not self.has_change_permission(request):
            return {}
        return actions

    def has_delete_permission(self, request, obj=None):
        """A booking that holds stock or has any payment entry cannot be deleted."""
        if obj is not None and (lifecycle.holds_stock(obj) or obj.payments.exists()):
            return False
        return super().has_delete_permission(request, obj)

    def has_resolve_attention_permission(self, request, object_id=None):
        """Owner and Office may resolve; on a booking page only while it is flagged."""
        if not request.user.has_perm("bookings.resolve_attention"):
            return False
        if object_id is None:
            return True
        return Booking.objects.filter(pk=object_id, needs_attention=True).exists()

    @action(
        description="Resolve attention",
        permissions=["resolve_attention"],
        url_path="resolve-attention",
    )
    def resolve_attention_detail(self, request, object_id):
        return redirect(
            reverse("admin:bookings_booking_ledger", args=[object_id, "resolve-attention"])
        )

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

    @admin.action(description="Send invoice to customer")
    def send_invoice_to_customer(self, request, queryset):
        """Email each selected booking's current invoice, with its PDF, to the customer."""
        sent, missing = [], []
        for booking in queryset.order_by("pk"):
            invoice = email_service.queue_latest_invoice(booking)
            (sent if invoice else missing).append(f"#{booking.pk}")
        if sent:
            self.message_user(
                request, f"Invoice emailed for booking {', '.join(sent)}.", messages.SUCCESS
            )
        if missing:
            self.message_user(
                request,
                f"No invoice yet for booking {', '.join(missing)}. Add lines or issue a "
                "manual invoice first.",
                messages.ERROR,
            )

    @admin.action(description="Resend last reminder")
    def resend_last_reminder(self, request, queryset):
        """Send each booking's latest reminder again (consent and quiet hours still apply)."""
        sent, none = [], []
        for booking in queryset.order_by("pk"):
            kind = reminders.resend_last(booking)
            (sent if kind else none).append(f"#{booking.pk}")
        if sent:
            self.message_user(
                request, f"Reminder sent again for booking {', '.join(sent)}.", messages.SUCCESS
            )
        if none:
            self.message_user(
                request,
                f"No reminder to resend for booking {', '.join(none)} (none sent yet, or it no "
                "longer applies).",
                messages.WARNING,
            )

    @admin.action(description="Issue manual invoice (deal agreed offline)")
    def issue_manual_invoice(self, request, queryset):
        return self._open_ledger_form(request, queryset, "manual-invoice")


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
