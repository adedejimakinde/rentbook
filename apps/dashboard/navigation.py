"""The staff navigation: a bottom tab bar on phones and a left sidebar on desktop."""

from dataclasses import dataclass

from django.urls import reverse


@dataclass
class NavItem:
    key: str
    label: str
    url: str
    icon: str
    active: bool = False


def nav_items(user, active):
    """The tabs this user may open, with the one called active marked."""
    entries = [
        ("home", "Home", reverse("admin:index"), "home", "bookings.view_dashboard"),
        (
            "calendar",
            "Calendar",
            reverse("dashboard:calendar"),
            "calendar",
            "bookings.view_dashboard",
        ),
        (
            "bookings",
            "Bookings",
            reverse("admin:bookings_booking_changelist"),
            "list",
            "bookings.view_booking",
        ),
        ("crew", "Crew", reverse("dashboard:crew"), "truck", "bookings.view_crew_schedule"),
        (
            "payments",
            "Payments",
            reverse("admin:payments_payment_changelist"),
            "credit-card",
            "payments.view_payment",
        ),
    ]
    return [
        NavItem(key, label, url, icon, active=key == active)
        for key, label, url, icon, permission in entries
        if user.has_perm(permission)
    ]


@dataclass
class BackOfficeLink:
    label: str
    url: str
    icon: str


def back_office_links(user):
    """Links to the rest of the admin, for the desktop sidebar."""
    entries = [
        ("Catalogue", "admin:app_list", {"app_label": "catalogue"}, "list", "catalogue.view_item"),
        (
            "Inventory",
            "admin:app_list",
            {"app_label": "inventory"},
            "truck",
            "inventory.view_stockadjustment",
        ),
        (
            "Invoices",
            "admin:app_list",
            {"app_label": "invoices"},
            "download",
            "invoices.view_invoice",
        ),
        (
            "Site settings",
            "admin:core_sitesettings_changelist",
            {},
            "settings",
            "core.change_sitesettings",
        ),
    ]
    return [
        BackOfficeLink(label, reverse(name, kwargs=kwargs), icon)
        for label, name, kwargs, icon, permission in entries
        if user.has_perm(permission)
    ]
