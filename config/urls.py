"""Root URL configuration."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.urls import include, path

from apps.catalogue.sitemaps import CategorySitemap, ItemSitemap, PackageSitemap
from apps.core.sitemaps import StaticPagesSitemap

SITEMAPS = {
    "pages": StaticPagesSitemap,
    "categories": CategorySitemap,
    "items": ItemSitemap,
    "packages": PackageSitemap,
}

urlpatterns = [
    path(settings.ADMIN_URL, admin.site.urls),
    path("sitemap.xml", sitemap, {"sitemaps": SITEMAPS}, name="sitemap"),
    path("payments/", include("apps.payments.urls")),
    path("", include("apps.bookings.urls")),
    path("", include("apps.catalogue.urls")),
    path("", include("apps.core.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
