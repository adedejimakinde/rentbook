"""Twilio: phone numbers, the opt-out list, and sending one SMS or WhatsApp message."""

import json
import re

from django.conf import settings
from django.utils import timezone

from apps.notifications.models import SmsOptOut

OPT_OUT_WORDS = {"STOP", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
OPT_IN_WORDS = {"START"}
# Twilio error codes that will never succeed on a retry.
UNSUBSCRIBED_ERROR = 21610
PERMANENT_ERRORS = {UNSUBSCRIBED_ERROR, 21211, 21614, 63016}


class SendError(Exception):
    """Twilio refused the message. permanent: retrying will not help."""

    def __init__(self, message, *, permanent=False, code=None):
        super().__init__(message)
        self.permanent = permanent
        self.code = code


def normalise_phone(raw):
    """A US phone number in E.164 (+14695550100), or "" if it cannot be one."""
    text = str(raw or "").strip()
    if text.lower().startswith("whatsapp:"):
        text = text[len("whatsapp:") :]
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if text.startswith("+") and 8 <= len(digits) <= 15:
        return f"+{digits}"
    return ""


def is_opted_out(phone):
    number = normalise_phone(phone)
    return bool(number) and SmsOptOut.objects.filter(phone=number, opted_out=True).exists()


def opt_out(phone, now=None):
    """Record that phone replied STOP (or Twilio says it is unsubscribed)."""
    number = normalise_phone(phone)
    if not number:
        return None
    row, _ = SmsOptOut.objects.update_or_create(
        phone=number, defaults={"opted_out": True, "opted_out_at": now or timezone.now()}
    )
    return row


def opt_in(phone, now=None):
    """Record that phone replied START."""
    number = normalise_phone(phone)
    if not number:
        return None
    row, _ = SmsOptOut.objects.update_or_create(
        phone=number, defaults={"opted_out": False, "opted_in_at": now or timezone.now()}
    )
    return row


def handle_inbound(from_number, body):
    """Act on a keyword reply. Returns "opted_out", "opted_in" or "" (anything else)."""
    word = str(body or "").strip().upper()
    if word in OPT_OUT_WORDS:
        opt_out(from_number)
        return "opted_out"
    if word in OPT_IN_WORDS:
        opt_in(from_number)
        return "opted_in"
    return ""


def client():
    """A Twilio REST client. Tests replace this."""
    from twilio.rest import Client

    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


def _create(**kwargs):
    from twilio.base.exceptions import TwilioException, TwilioRestException

    try:
        message = client().messages.create(**kwargs)
    except TwilioRestException as error:
        raise SendError(
            f"Twilio error {error.code}: {error.msg}",
            permanent=error.code in PERMANENT_ERRORS,
            code=error.code,
        ) from error
    except TwilioException as error:
        raise SendError(str(error)) from error
    return message.sid


def send_sms(to, body):
    """Send one SMS. Returns Twilio's message SID."""
    return _create(to=normalise_phone(to), from_=settings.TWILIO_FROM_NUMBER, body=body)


def send_whatsapp(to, template_sid, variables):
    """Send one business-initiated WhatsApp message from an approved template."""
    sender = settings.TWILIO_WHATSAPP_FROM
    if not sender.startswith("whatsapp:"):
        sender = f"whatsapp:{normalise_phone(sender)}"
    return _create(
        to=f"whatsapp:{normalise_phone(to)}",
        from_=sender,
        content_sid=template_sid,
        content_variables=json.dumps(variables),
    )


def validate_signature(url, params, signature):
    """True when the request really came from Twilio (signed with our auth token)."""
    from twilio.request_validator import RequestValidator

    token = settings.TWILIO_AUTH_TOKEN
    if not token or not signature:
        return False
    return RequestValidator(token).validate(url, params, signature)
