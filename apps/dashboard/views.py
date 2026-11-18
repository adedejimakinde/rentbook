"""Owner dashboard, calendar and crew schedule. Read-only views; actions call the services."""

from datetime import date, timedelta
from functools import wraps

from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.bookings.models import Booking
from apps.core import site_settings
from apps.dashboard import navigation
from apps.dashboard.services import calendar as calendar_service
from apps.dashboard.services import crew, overview, runsheet, switches
from apps.dashboard.services.visibility import visibility_for

DASHBOARD = "bookings.view_dashboard"
CREW = "bookings.view_crew_schedule"
ACTIONS = ("delivered", "returned")


def staff_view(permission):
    """Staff only (the admin login), and only with the given permission."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.has_perm(permission):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return admin.site.admin_view(wrapper)

    return decorator


def is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _first_name(user):
    return user.first_name or user.get_username()


def _context(request, active, **extra):
    return {
        "nav": navigation.nav_items(request.user, active),
        "back_office": navigation.back_office_links(request.user),
        "active": active,
        "brand_short": settings.BUSINESS_NAME.split()[0],
        **extra,
    }


def dashboard(request):
    """The admin home. Crew-only staff are sent to today's crew schedule instead."""
    if not request.user.has_perm(DASHBOARD):
        if request.user.has_perm(CREW):
            return redirect("dashboard:crew")
        raise PermissionDenied
    data = overview.build()
    context = _context(
        request,
        "home",
        data=data,
        first_name=_first_name(request.user),
        **_switch_context(request),
    )
    return render(request, "dashboard/home.html", context)


def _switch_context(request):
    allowed = switches.can_view(request.user)
    return {
        "show_switches": allowed,
        "switch_rows": switches.rows() if allowed else [],
        "can_change_switches": switches.can_change(request.user),
    }


@require_POST
@staff_view(switches.CHANGE_PERMISSION)
def switch(request):
    """Turn one quick switch on or off. The change is logged with who made it."""
    name = request.POST.get("switch", "")
    if name not in site_settings.QUICK_SWITCHES:
        raise Http404
    site_settings.set_switch(name, request.POST.get("on") == "1", by=request.user)
    if is_htmx(request):
        return render(request, "dashboard/_quick_switches.html", _switch_context(request))
    return redirect("admin:index")


@staff_view(DASHBOARD)
def calendar_page(request):
    """Month or week calendar of bookings by event date. HTMX swaps the body."""
    today = timezone.localdate()
    view = "week" if request.GET.get("view") == "week" else "month"
    anchor = calendar_service.parse_anchor(request.GET.get("date"), today)
    selected = (
        calendar_service.parse_anchor(request.GET.get("day"), None)
        if request.GET.get("day")
        else None
    )
    page = calendar_service.build(view, anchor, selected)
    template = "dashboard/_calendar_body.html" if is_htmx(request) else "dashboard/calendar.html"
    return render(request, template, _context(request, "calendar", page=page))


def _parse_day(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise Http404 from error


@staff_view(CREW)
def crew_today(request):
    return redirect("dashboard:crew_day", day=timezone.localdate().isoformat())


@staff_view(CREW)
def crew_day(request, day):
    """Every delivery and pickup on one day, in time order."""
    day = _parse_day(day)
    cards = crew.schedule(day, request.user)
    context = _context(
        request,
        "crew",
        day=day,
        cards=cards,
        previous_day=day - timedelta(days=1),
        next_day=day + timedelta(days=1),
        is_today=day == timezone.localdate(),
    )
    return render(request, "dashboard/crew_day.html", context)


@staff_view(CREW)
def run_sheet(request, day):
    """The day's run sheet as a PDF with a checklist for each stop."""
    day = _parse_day(day)
    pdf = runsheet.render_pdf(day, request.user)
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="run-sheet-{day.isoformat()}.pdf"'
    return response


@require_POST
@staff_view(CREW)
def crew_mark(request, booking_id):
    """Mark a booking delivered or returned, through the lifecycle services."""
    action = request.POST.get("action")
    if action not in ACTIONS:
        raise Http404
    day = _parse_day(request.POST.get("day"))
    kind = request.POST.get("kind", "")
    if not Booking.objects.filter(pk=booking_id).exists():
        raise Http404
    if action == "delivered":
        show_money = visibility_for(request.user).show_money
        result = crew.mark_delivered(
            booking_id,
            by=request.user,
            show_money=show_money,
            override_note=request.POST.get("override_note", ""),
        )
    else:
        result = crew.mark_returned(booking_id)
    card = crew.card_for(booking_id, kind, day, request.user)
    if card is not None and not result.ok:
        card.message = result.message
        card.needs_override = result.needs_override
    if is_htmx(request) and card is not None:
        return render(request, "dashboard/_stop_card.html", {"card": card, "day": day})
    messages.add_message(request, messages.SUCCESS if result.ok else messages.ERROR, result.message)
    return redirect("dashboard:crew_day", day=day.isoformat())
