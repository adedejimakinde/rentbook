"""Sitemap for the fixed public pages."""

from django.contrib.sitemaps import Sitemap
from django.urls import reverse


class StaticPagesSitemap(Sitemap):
    changefreq = "monthly"
    priority = 0.5

    PAGES = (
        "core:home",
        "catalogue:list",
        "catalogue:packages",
        "catalogue:gallery",
        "core:about",
        "core:faq",
        "core:contact",
        "core:links",
    )

    def items(self):
        return self.PAGES

    def location(self, item):
        return reverse(item)
