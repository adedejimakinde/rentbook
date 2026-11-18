"""Staff URLs. Mounted under the admin URL, ahead of the admin's own catch-all."""

from django.contrib import admin
from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", admin.site.admin_view(views.dashboard), name="home"),
    path("calendar/", views.calendar_page, name="calendar"),
    path("crew/", views.crew_today, name="crew"),
    path("crew/<str:day>/", views.crew_day, name="crew_day"),
    path("crew/<str:day>/run-sheet.pdf", views.run_sheet, name="run_sheet"),
    path("crew/mark/<int:booking_id>/", views.crew_mark, name="crew_mark"),
    path("switch/", views.switch, name="switch"),
]
