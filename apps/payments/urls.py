"""URLs for payments."""

from django.urls import path

from apps.payments import views

app_name = "payments"

urlpatterns = [
    path("stripe/webhook/", views.stripe_webhook, name="stripe_webhook"),
]
