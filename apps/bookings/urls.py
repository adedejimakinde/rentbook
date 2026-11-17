"""Public cart and booking flow URLs."""

from django.urls import path

from apps.bookings import views

app_name = "bookings"

urlpatterns = [
    path("cart/", views.cart_page, name="cart"),
    path("cart/add/", views.cart_add, name="cart_add"),
    path("cart/update/", views.cart_update, name="cart_update"),
    path("cart/remove/", views.cart_remove, name="cart_remove"),
    path("cart/details/", views.cart_details, name="cart_details"),
    path("book/<slug:step>/", views.step, name="step"),
    path("booking/<str:token>/", views.status, name="status"),
]
