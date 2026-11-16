"""Admin for the catalogue."""

from django.contrib import admin, messages
from unfold.admin import ModelAdmin, TabularInline

from apps.catalogue.models import Category, Item, ItemVariant, Package, PackageItem
from apps.inventory import services as inventory


@admin.register(Category)
class CategoryAdmin(ModelAdmin):
    list_display = ["name", "slug"]
    search_fields = ["name"]
    prepopulated_fields = {"slug": ["name"]}


class ItemVariantInline(TabularInline):
    """Variants are edited on their item; every item keeps at least one."""

    model = ItemVariant
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
        for variant in formset.deleted_objects:
            variant.delete()
        warning = inventory.shortage_warning(inventory.save_variants(variants))
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
