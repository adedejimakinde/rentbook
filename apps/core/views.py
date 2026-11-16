"""Site-level views."""

from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

RENTAL_CATEGORIES = [
    "Chairs",
    "Tables",
    "Linen",
    "Bounce houses",
    "Backdrops",
    "Golf simulator",
    "Popcorn machines",
    "Cotton candy machines",
]


@require_GET
def home(request):
    """Render the public home page."""
    return render(request, "core/home.html", {"rentals": RENTAL_CATEGORIES})


@never_cache
@require_GET
def health(request):
    """Report liveness and database connectivity for load balancers and uptime checks."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "error", "database": "down"}, status=503)
    return JsonResponse({"status": "ok", "database": "up"})
