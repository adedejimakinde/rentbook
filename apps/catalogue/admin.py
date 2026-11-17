"""Admin for the catalogue."""

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.forms import BaseInlineFormSet
from unfold.admin import ModelAdmin, TabularInline

from apps.catalogue.models import Category, Item, ItemImage, ItemVariant, Package, PackageItem
from apps.inventory import services as inventory


@admin.register(Category)
class CategoryAdmin(ModelAdmin):
    list_display = ["name", "slug", "sort_order"]
    search_fields = ["name"]
    prepopulated_fields = {"slug": ["name"]}


class ItemVariantFormSet(BaseInlineFormSet):
    """Validates changed and deleted variants again under their row locks.

    The admin change view runs in one transaction, so the locks taken here
    are held until the variants are saved, and anything that became invalid
    meanwhile (units marked damaged, a variant booked) is a form error.
    """

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        changed = [
            form.instance
            for form in self.forms
            if form.has_changed() and form not in self.deleted_forms
        ]
        deleted = [form.instance for form in self.deleted_forms if form.instance.pk]
        inventory.check_variants_under_lock(changed, deleted, exclude=[self.fk.name])


class ItemVariantInline(TabularInline):
    """Variants are edited on their item; every item keeps at least one."""

    model = ItemVariant
    formset = ItemVariantFormSet
    fields = ["name", "price_override", "stock_quantity", "stock_confirmed"]
    extra = 0
    min_num = 1

    def get_formset(self, request, obj=None, **kwargs):
        """Reject the form, not just pre-fill a row, when no variant is left."""
        return super().get_formset(request, obj, validate_min=True, **kwargs)


class ItemImageFormSet(forms.BaseInlineFormSet):
    """At most one image per item may be primary."""

    def clean(self):
        super().clean()
        primaries = [
            form
            for form in self.forms
            if form.cleaned_data
            and not form.cleaned_data.get("DELETE")
            and form.cleaned_data.get("is_primary")
        ]
        if len(primaries) > 1:
            raise ValidationError("Only one image can be the primary image.")


class ItemImageInline(TabularInline):
    """Photos of an item. Sizes are generated in the background after saving."""

    model = ItemImage
    formset = ItemImageFormSet
    fields = ["image", "alt_text", "sort_order", "is_primary"]
    extra = 0


class ItemAdminForm(forms.ModelForm):
    """Edit the JSON lists as plain text: one bullet per line, one "Name: value" spec per line."""

    whats_included_text = forms.CharField(
        label="What's included",
        required=False,
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="One item per line.",
    )
    specs_text = forms.CharField(
        label="Specs",
        required=False,
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text='One per line as "Name: value", for example "Size: 13 x 13 ft".',
    )

    class Meta:
        model = Item
        fields = [
            "category",
            "name",
            "slug",
            "short_description",
            "description",
            "whats_included_text",
            "specs_text",
            "base_price",
            "caution_fee",
            "is_featured",
            "needs_power",
            "is_active",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["whats_included_text"] = "\n".join(self.instance.whats_included)
            self.initial["specs_text"] = "\n".join(
                f"{spec['name']}: {spec['value']}" for spec in self.instance.specs
            )

    def clean(self):
        cleaned = super().clean()
        included = [
            line.strip()
            for line in cleaned.get("whats_included_text", "").splitlines()
            if line.strip()
        ]
        specs = []
        for line in cleaned.get("specs_text", "").splitlines():
            if not line.strip():
                continue
            name, separator, value = line.partition(":")
            if not separator or not name.strip() or not value.strip():
                self.add_error("specs_text", f'"{line.strip()}" is not in the form "Name: value".')
                continue
            specs.append({"name": name.strip(), "value": value.strip()})
        self.instance.whats_included = included
        self.instance.specs = specs
        return cleaned


@admin.register(Item)
class ItemAdmin(ModelAdmin):
    form = ItemAdminForm
    list_display = ["name", "category", "base_price", "caution_fee", "is_featured", "is_active"]
    list_filter = ["is_active", "is_featured", "category"]
    list_select_related = ["category"]
    search_fields = ["name", "description"]
    prepopulated_fields = {"slug": ["name"]}
    inlines = [ItemVariantInline, ItemImageInline]

    def save_formset(self, request, form, formset, change):
        """Save variants under their row locks and warn about bookings left short."""
        if formset.model is not ItemVariant:
            return super().save_formset(request, form, formset, change)
        variants = formset.save(commit=False)
        short = inventory.save_variants(variants, deleted=formset.deleted_objects)
        warning = inventory.shortage_warning(short)
        formset.save_m2m()
        if warning:
            self.message_user(request, warning, messages.WARNING)
        return None


@admin.register(ItemVariant)
class ItemVariantAdmin(ModelAdmin):
    """Read-only lookup used by autocomplete fields; variants are edited on the item."""

    list_display = ["__str__", "stock_quantity"]
    list_select_related = ["item"]
    search_fields = ["name", "item__name"]

    def has_module_permission(self, request):
        return False

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class PackageItemInline(TabularInline):
    """The variants a package reserves; every package keeps at least one."""

    model = PackageItem
    fields = ["variant", "quantity"]
    autocomplete_fields = ["variant"]
    extra = 0
    min_num = 1

    def get_formset(self, request, obj=None, **kwargs):
        """Reject the form, not just pre-fill a row, when no item is left."""
        return super().get_formset(request, obj, validate_min=True, **kwargs)


@admin.register(Package)
class PackageAdmin(ModelAdmin):
    list_display = ["name", "package_price", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name"]
    prepopulated_fields = {"slug": ["name"]}
    inlines = [PackageItemInline]
