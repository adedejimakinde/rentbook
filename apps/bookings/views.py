"""Public cart and booking flow. Views stay thin: prices come from build_quote via the
cart service, bookings from create_booking_from_quote, checkouts from
create_checkout_session."""

import logging
from datetime import date, time

import stripe
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from apps.bookings import flow_forms
from apps.bookings.models import normalise_zip
from apps.bookings.services import booking_flow, lifecycle, links
from apps.bookings.services import cart as cart_service
from apps.bookings.services import quote as quote_service
from apps.core.services import ZIP_KEY, get_event_date, set_event_date
from apps.payments.services import ledger
from apps.payments.services import stripe as stripe_service
from apps.payments.services.status import status_summary

logger = logging.getLogger(__name__)

STEP_FORMS = {
    booking_flow.EVENT: flow_forms.EventForm,
    booking_flow.DELIVERY: flow_forms.DeliveryForm,
    booking_flow.CONTACT: flow_forms.ContactForm,
}


def is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _cart_context(request, **extra):
    return {
        "cart": cart_service.cart_view(request),
        "htmx": is_htmx(request),
        "meta_title": "Your booking cart",
        **extra,
    }


@never_cache
@require_http_methods(["GET"])
def cart_page(request):
    """The cart with its live quote."""
    return render(request, "bookings/cart.html", _cart_context(request))


def _cart_body(request, **extra):
    return render(request, "bookings/_cart_body.html", _cart_context(request, **extra))


@require_POST
def cart_add(request):
    """Add a variant or package. HTMX gets a short confirmation and the new cart count."""
    try:
        cart_service.add(
            request, request.POST.get("kind"), request.POST.get("id"), request.POST.get("quantity")
        )
    except cart_service.CartError as error:
        if not is_htmx(request):
            messages.error(request, str(error))
            return redirect("bookings:cart")
        return render(request, "bookings/_added.html", {"error": str(error)}, status=422)
    if not is_htmx(request):
        return redirect("bookings:cart")
    return render(request, "bookings/_added.html", {})


@require_POST
def cart_update(request):
    """Change a line's quantity and return the refreshed cart."""
    error = ""
    try:
        cart_service.update(
            request, request.POST.get("kind"), request.POST.get("id"), request.POST.get("quantity")
        )
    except cart_service.CartError as problem:
        error = str(problem)
    if not is_htmx(request):
        if error:
            messages.error(request, error)
        return redirect("bookings:cart")
    return _cart_body(request, update_error=error)


@require_POST
def cart_remove(request):
    """Remove a line and return the refreshed cart."""
    cart_service.remove(request, request.POST.get("kind"), request.POST.get("id"))
    if not is_htmx(request):
        return redirect("bookings:cart")
    return _cart_body(request)


@require_POST
def cart_details(request):
    """Set the event date, delivery ZIP and promo code the quote is built for."""
    date_error = set_event_date(request, request.POST.get("date", ""))
    zip_code = request.POST.get("zip_code", "").strip()
    zip_error = ""
    if zip_code:
        cleaned = normalise_zip(zip_code)
        if cleaned:
            request.session[ZIP_KEY] = cleaned
        else:
            zip_error = "Enter a 5-digit ZIP code."
    cart_service.set_promo(request, request.POST.get("promo_code", ""))
    if not is_htmx(request):
        for error in (date_error, zip_error):
            if error:
                messages.error(request, error)
        return redirect("bookings:cart")
    return _cart_body(request, date_error=date_error, zip_error=zip_error)


def _initial(request, step):
    """Prefill a step from what was entered before, or from the cart's date and ZIP."""
    saved = booking_flow.get_flow(request).get(step)
    if saved:
        return saved
    if step == booking_flow.EVENT:
        event_date = get_event_date(request)
        return {"event_date": event_date.isoformat()} if event_date else {}
    if step == booking_flow.DELIVERY:
        return {"zip_code": request.session.get(ZIP_KEY, ""), "state": "TX"}
    return {}


def _guard(request, step):
    """Redirect when the cart is empty or an earlier step is still to do, else None."""
    if not cart_service.get_lines(request):
        messages.info(request, "Add rentals to your cart before booking.")
        return redirect("bookings:cart")
    first = booking_flow.first_incomplete_step(request)
    if booking_flow.STEPS.index(step) > booking_flow.STEPS.index(first):
        return redirect("bookings:step", step=first)
    return None


def _flow_context(step, **extra):
    index = booking_flow.STEPS.index(step)
    return {
        "step": step,
        "step_number": index + 1,
        "step_count": len(booking_flow.STEPS),
        "step_title": booking_flow.STEP_TITLES[step],
        "progress": round((index + 1) * 100 / len(booking_flow.STEPS)),
        "previous_step": booking_flow.STEPS[index - 1] if index else None,
        "meta_title": f"Book: {booking_flow.STEP_TITLES[step]}",
        **extra,
    }


@never_cache
@require_http_methods(["GET", "POST"])
def step(request, step):
    """One details step of the booking flow: event, delivery or contact."""
    if step == booking_flow.REVIEW:
        return review(request)
    if step not in STEP_FORMS:
        raise Http404
    blocked = _guard(request, step)
    if blocked:
        return blocked
    form_class = STEP_FORMS[step]
    if request.method == "POST":
        form = form_class(request.POST)
        if form.is_valid():
            data = form.to_session()
            booking_flow.save_step(request, step, data)
            if step == booking_flow.EVENT:
                set_event_date(request, data["event_date"])
            elif step == booking_flow.DELIVERY:
                request.session[ZIP_KEY] = data["zip_code"]
            following = booking_flow.STEPS[booking_flow.STEPS.index(step) + 1]
            return redirect("bookings:step", step=following)
    else:
        form = form_class(initial=_initial(request, step))
    return render(request, "bookings/step.html", _flow_context(step, form=form))


def _absolute(path):
    return f"{settings.SITE_URL}{path}"


def _checkout_urls(booking):
    success = reverse("payments:success", args=[links.make_token(booking, links.SUCCESS)])
    cancel = reverse("payments:cancel", args=[links.make_token(booking, links.SUCCESS)])
    return {
        "success_url": _absolute(success) + "?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": _absolute(cancel),
    }


def _review_context(request, form, **extra):
    flow = booking_flow.get_flow(request)
    view = cart_service.cart_view(request)
    return _flow_context(
        booking_flow.REVIEW,
        form=form,
        cart=view,
        flow=flow,
        address=booking_flow.format_address(flow[booking_flow.DELIVERY]),
        event_type_label=dict(flow_forms.EventType.choices).get(
            flow[booking_flow.EVENT]["event_type"], ""
        ),
        surface_label=dict(flow_forms.Surface.choices).get(
            flow[booking_flow.DELIVERY]["surface"], ""
        ),
        start_time=time.fromisoformat(flow[booking_flow.EVENT]["start_time"]),
        setup_by_time=time.fromisoformat(flow[booking_flow.EVENT]["setup_by_time"]),
        manual_hold_hours=settings.MANUAL_PAY_HOLD_HOURS,
        **extra,
    )


@never_cache
def review(request):
    """Last step: the full quote again, terms, how much to pay and how."""
    blocked = _guard(request, booking_flow.REVIEW)
    if blocked:
        return blocked
    if request.method != "POST":
        context = _review_context(request, flow_forms.ReviewForm())
        return render(request, "bookings/review.html", context)

    form = flow_forms.ReviewForm(request.POST)
    if not form.is_valid():
        return render(request, "bookings/review.html", _review_context(request, form), status=422)

    earlier = booking_flow.release_pending(request)
    if earlier is not None:
        messages.info(request, "This booking already has a payment on it.")
        return redirect("bookings:status", token=links.make_token(earlier, links.STATUS))

    flow = booking_flow.get_flow(request)
    event_date = flow[booking_flow.EVENT]["event_date"]
    plan, choice = form.cleaned_data["plan"], form.cleaned_data["choice"]
    try:
        booking = booking_flow.submit_booking(
            cart_service.get_lines(request),
            event_date=date.fromisoformat(event_date),
            zip_code=flow[booking_flow.DELIVERY]["zip_code"],
            promo_code=cart_service.get_promo(request),
            flow=flow,
            plan=plan,
            choice=choice,
        )
    except quote_service.QuoteNotBookable:
        context = _review_context(
            request, form, submit_error="Your quote changed. Please check the items below."
        )
        return render(request, "bookings/review.html", context, status=409)

    if booking_flow.is_manual(choice):
        cart_service.clear(request)
        booking_flow.clear_flow(request)
        return redirect("payments:manual", token=links.make_token(booking, links.MANUAL))

    try:
        session = stripe_service.create_checkout_session(
            booking, booking_flow.PLAN_KINDS[plan], **_checkout_urls(booking)
        )
    except (ledger.PaymentError, stripe.StripeError, ImproperlyConfigured):
        logger.exception("Could not start Stripe Checkout for booking %s", booking.pk)
        try:
            lifecycle.cancel_booking(booking)
        except lifecycle.BookingStateError:
            logger.exception("Could not release booking %s after checkout failed", booking.pk)
        context = _review_context(
            request,
            form,
            submit_error="We could not start the card payment. Nothing was charged. Please try "
            "again, or choose Zelle, Cash App or bank deposit.",
        )
        return render(request, "bookings/review.html", context, status=502)
    booking_flow.remember_pending(request, booking)
    return HttpResponseRedirect(session["url"], status=303)


@never_cache
def status(request, token):
    """Booking status by signed link: items, date, address, amounts paid and due."""
    try:
        booking = links.read_token(token, links.STATUS)
    except links.ExpiredLink:
        return render(request, "bookings/link_invalid.html", {"expired": True}, status=410)
    except links.BadLink:
        return render(request, "bookings/link_invalid.html", {}, status=404)
    context = status_summary(booking)
    context["meta_title"] = f"Booking #{booking.pk}"
    return render(request, "bookings/status.html", context)
