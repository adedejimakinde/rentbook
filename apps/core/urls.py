"""URLs for site pages and the health check."""

from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("check/", views.quick_check, name="quick_check"),
    path("about/", views.about, name="about"),
    path("faq/", views.faq, name="faq"),
    path("contact/", views.contact, name="contact"),
    path("links/", views.links, name="links"),
    path("site.webmanifest", views.manifest, name="manifest"),
    path("rental-terms/", views.terms, name="terms"),
    path("privacy/", views.privacy, name="privacy"),
    path("robots.txt", views.robots, name="robots"),
    path("health/", views.health, name="health"),
]
