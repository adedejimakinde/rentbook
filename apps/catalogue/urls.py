"""Public catalogue URLs."""

from django.urls import path

from . import views

app_name = "catalogue"

urlpatterns = [
    path("rentals/", views.catalogue, name="list"),
    path("rentals/<slug:category_slug>/", views.catalogue, name="category"),
    path("items/<slug:slug>/", views.item_detail, name="item"),
    path("items/<slug:slug>/availability/", views.item_availability, name="availability"),
    path("packages/", views.package_list, name="packages"),
    path("packages/<slug:slug>/", views.package_detail, name="package"),
    path("gallery/", views.gallery, name="gallery"),
]
