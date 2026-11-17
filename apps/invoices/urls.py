"""Customer document downloads."""

from django.urls import path

from apps.invoices import views

app_name = "invoices"

urlpatterns = [
    path(
        "booking/<str:token>/documents/<str:number>.pdf",
        views.customer_pdf,
        name="customer_pdf",
    ),
]
