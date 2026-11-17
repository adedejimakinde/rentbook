"""Notification webhooks."""

from django.urls import path

from apps.notifications import views

app_name = "notifications"

urlpatterns = [
    path("twilio/inbound/", views.twilio_inbound, name="twilio_inbound"),
]
