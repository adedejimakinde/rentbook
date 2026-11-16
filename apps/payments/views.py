"""Payment views. Only the Stripe webhook; bookings are never marked paid elsewhere."""

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.payments.services import stripe as stripe_service


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
