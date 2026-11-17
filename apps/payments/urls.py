"""URLs for payments: the Stripe webhook and the customer payment pages."""

from django.urls import path

from apps.payments import views

app_name = "payments"

urlpatterns = [
    path("stripe/webhook/", views.stripe_webhook, name="stripe_webhook"),
    path("manual/<str:token>/", views.manual, name="manual"),
    path("success/<str:token>/", views.success, name="success"),
    path("success/<str:token>/status/", views.success_status, name="success_status"),
    path("cancel/<str:token>/", views.cancel, name="cancel"),
    path("balance/<str:token>/", views.balance, name="balance"),
]
