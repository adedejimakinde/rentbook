"""Gives admin pages the same phone tab bar as the dashboard."""

from apps.dashboard import navigation

ACTIVE_BY_URL_NAME = {
    "bookings_booking": "bookings",
    "payments_payment": "payments",
}


def staff_nav(request):
    """The tab bar for admin pages (not the public site). Empty when not signed in as staff."""
    user = getattr(request, "user", None)
    match = getattr(request, "resolver_match", None)
    if not (user and user.is_active and user.is_staff and match):
        return {}
    if match.namespace != "admin":
        return {}
    active = next(
        (key for prefix, key in ACTIVE_BY_URL_NAME.items() if match.url_name.startswith(prefix)),
        "",
    )
    return {"staff_tabs": navigation.nav_items(user, active)}
