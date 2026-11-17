"""Payment views. Bookings are marked paid only by the Stripe webhook, never by these pages."""

import logging

import stripe
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.bookings.models import PaymentChoice, PaymentPlan
from apps.bookings.services import booking_flow, links
from apps.bookings.services import cart as cart_service
from apps.payments.models import Payment
from apps.payments.services import ledger
from apps.payments.services import status as payment_status
from apps.payments.services import stripe as stripe_service

logger = logging.getLogger(__name__)

# Polls of the success page before it stops and says the confirmation is on its way.
MAX_POLLS = 40


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Receive a Stripe event. Rejects any request without a valid signature."""
    try:
        event = stripe_service.parse_event(
            request.body, request.headers.get("Stripe-Signature", "")
        )
    except stripe_service.InvalidWebhookError:
        return HttpResponse(status=400)
    stripe_service.handle_event(event)
    return HttpResponse(status=200)


def _booking_or_error(request, token, purpose):
    """(booking, None), or (None, an error page) for a bad or expired link."""
    try:
        return links.read_token(token, purpose), None
    except links.ExpiredLink:
        return None, render(request, "bookings/link_invalid.html", {"expired": True}, status=410)
    except links.BadLink:
        return None, render(request, "bookings/link_invalid.html", {}, status=404)


def manual_details():
    """Zelle, Cash App and bank details from settings, for the instructions page."""
    return {
        PaymentChoice.ZELLE: [
            ("Send to", settings.ZELLE_RECIPIENT),
            ("Name shown", settings.ZELLE_NAME),
        ],
        PaymentChoice.CASHAPP: [("Cash App tag", settings.CASHAPP_TAG)],
        PaymentChoice.BANK_DEPOSIT: [
            ("Bank", settings.BANK_NAME),
            ("Account name", settings.BANK_ACCOUNT_NAME),
            ("Account number", settings.BANK_ACCOUNT_NUMBER),
            ("Routing number", settings.BANK_ROUTING_NUMBER),
        ],
    }


@never_cache
@require_GET
def manual(request, token):
    """How to pay by Zelle, Cash App or bank deposit. Staff record the payment when it arrives."""
    booking, error = _booking_or_error(request, token, links.MANUAL)
    if error:
        return error
    plan = booking.payment_plan or PaymentPlan.DEPOSIT
    try:
        amount, caution = booking_flow.amount_to_pay(booking, plan)
    except ledger.PaymentError:
        amount = caution = None
    choice = booking.payment_choice or PaymentChoice.ZELLE
    context = {
        "booking": booking,
        "reference": f"Booking {booking.pk}",
        "amount": amount,
        "caution": caution,
        "to_send": amount + caution if amount is not None else None,
        "plan_label": PaymentPlan(plan).label,
        "choice_label": PaymentChoice(choice).label,
        "details": manual_details().get(choice, []),
        "deadline": booking.hold_expires_at,
        "status_url": reverse("bookings:status", args=[links.make_token(booking, links.STATUS)]),
        "meta_title": "How to pay",
    }
    return render(request, "payments/manual.html", context)


def _success_context(request, booking, token):
    session_id = request.GET.get("session_id", "")[:255]
    try:
        polls = max(0, int(request.GET.get("n", 0)))
    except ValueError:
        polls = 0
    state = payment_status.checkout_state(booking, session_id)
    return {
        "booking": booking,
        "state": state,
        "token": token,
        "session_id": session_id,
        "next_poll": polls + 1,
        "keep_polling": state == payment_status.CONFIRMING and polls < MAX_POLLS,
        "gave_up": state == payment_status.CONFIRMING and polls >= MAX_POLLS,
        "status_url": reverse("bookings:status", args=[links.make_token(booking, links.STATUS)]),
    }


@never_cache
@require_GET
def success(request, token):
    """After Stripe Checkout. Shows what the webhook has recorded; never marks anything paid."""
    booking, error = _booking_or_error(request, token, links.SUCCESS)
    if error:
        return error
    if request.session.get(booking_flow.PENDING_BOOKING_KEY) == booking.pk:
        cart_service.clear(request)
        booking_flow.clear_flow(request)
    context = _success_context(request, booking, token)
    context["meta_title"] = "Thank you"
    return render(request, "payments/success.html", context)


@never_cache
@require_GET
def success_status(request, token):
    """HTMX partial polled by the success page until the webhook has recorded the payment."""
    booking, error = _booking_or_error(request, token, links.SUCCESS)
    if error:
        return error
    return render(
        request, "payments/_payment_status.html", _success_context(request, booking, token)
    )


@require_GET
def cancel(request, token):
    """The customer left Stripe Checkout. Back to review with the cart as it was."""
    booking, error = _booking_or_error(request, token, links.SUCCESS)
    if error:
        return redirect("bookings:cart")
    messages.info(request, "Payment cancelled. Nothing was charged. You can try again below.")
    return redirect("bookings:step", step=booking_flow.REVIEW)


@never_cache
@require_http_methods(["GET", "POST"])
def balance(request, token):
    """Pay the balance on a booking by signed, expiring link (sent by email)."""
    booking, error = _booking_or_error(request, token, links.BALANCE)
    if error:
        return error
    summary = payment_status.status_summary(booking)
    context = {**summary, "token": token, "meta_title": "Pay your balance"}
    try:
        amount, caution = stripe_service.amount_due(booking, Payment.Kind.BALANCE)
    except ledger.PaymentError as problem:
        context["cannot_pay"] = str(problem)
        return render(request, "payments/balance.html", context)
    context.update({"amount": amount, "caution": caution, "to_pay": amount + caution})
    if request.method == "POST":
        success_token = links.make_token(booking, links.SUCCESS)
        urls = {
            "success_url": f"{settings.SITE_URL}"
            f"{reverse('payments:success', args=[success_token])}"
            "?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": f"{settings.SITE_URL}{reverse('payments:balance', args=[token])}",
        }
        try:
            session = stripe_service.create_checkout_session(booking, Payment.Kind.BALANCE, **urls)
        except ledger.PaymentError as problem:
            context["cannot_pay"] = str(problem)
            return render(request, "payments/balance.html", context, status=409)
        except (stripe.StripeError, ImproperlyConfigured):
            logger.exception("Could not start balance checkout for booking %s", booking.pk)
            context["cannot_pay"] = "We could not start the payment. Please try again shortly."
            return render(request, "payments/balance.html", context, status=502)
        return HttpResponseRedirect(session["url"], status=303)
    return render(request, "payments/balance.html", context)
