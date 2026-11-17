"""Sitemap entries for categories, items and packages."""

from django.contrib.sitemaps import Sitemap

from apps.catalogue.models import Category, Item, Package


class CategorySitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.8

    def items(self):
        return Category.objects.filter(items__is_active=True).distinct()

    def lastmod(self, obj):
        return obj.updated_at


class ItemSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.7

    def items(self):
        return Item.objects.filter(is_active=True)

    def lastmod(self, obj):
        return obj.updated_at


class PackageSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.6

    def items(self):
        return Package.objects.filter(is_active=True, items__isnull=False).distinct()

    def lastmod(self, obj):
        return obj.updated_at
