"""Twilio inbound messages: STOP and START keywords. Every request must be signed by Twilio."""

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.notifications.services import sms

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


@csrf_exempt
@require_POST
def twilio_inbound(request):
    """Record opt-outs and opt-ins. Twilio sends the STOP and START replies itself."""
    signature = request.headers.get("X-Twilio-Signature", "")
    if not sms.validate_signature(request.build_absolute_uri(), request.POST.dict(), signature):
        return HttpResponse(status=403)
    sms.handle_inbound(request.POST.get("From", ""), request.POST.get("Body", ""))
    return HttpResponse(EMPTY_TWIML, content_type="text/xml")
