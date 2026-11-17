"""Template context for the header cart link."""

from django.conf import settings

from apps.bookings.services import cart as cart_service


def cart(request):
    """Cart line count and deposit percent. Reads the session, never the database."""
    count = cart_service.count(request) if hasattr(request, "session") else 0
    return {"cart_count": count, "deposit_percent": settings.DEPOSIT_PERCENT}
