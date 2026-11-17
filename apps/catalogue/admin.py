"""Admin for the catalogue."""

from django.contrib import admin, messages
from django.forms import BaseInlineFormSet
from unfold.admin import ModelAdmin, TabularInline

from apps.catalogue.models import Category, Item, ItemVariant, Package, PackageItem
from apps.inventory import services as inventory


@admin.register(Category)
class CategoryAdmin(ModelAdmin):
    list_display = ["name", "slug"]
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
    fields = ["name", "price_override", "stock_quantity"]
    extra = 0
    min_num = 1

    def get_formset(self, request, obj=None, **kwargs):
        """Reject the form, not just pre-fill a row, when no variant is left."""
        return super().get_formset(request, obj, validate_min=True, **kwargs)


@admin.register(Item)
class ItemAdmin(ModelAdmin):
    list_display = ["name", "category", "base_price", "caution_fee", "is_active"]
    list_filter = ["is_active", "category"]
    list_select_related = ["category"]
    search_fields = ["name", "description"]
    prepopulated_fields = {"slug": ["name"]}
    inlines = [ItemVariantInline]

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
