"""Background job that sends one email, retried with backoff if the mail service fails."""

from celery import shared_task

from apps.notifications.services import dispatch, emails, events, reminders, sms


@shared_task(
    name="notifications.send_email",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
    ignore_result=True,
)
def send_email(kind, object_id, key_suffix=""):
    """Build and send one email of kind for object_id, unless it was already sent."""
    emails.send(kind, object_id, key_suffix)


@shared_task(
    name="notifications.deliver",
    autoretry_for=(sms.SendError,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=dispatch.MAX_ATTEMPTS,
    ignore_result=True,
)
def deliver(notification_id):
    """Send one queued SMS or WhatsApp message, unless it was sent or is no longer due."""
    dispatch.deliver(notification_id)


@shared_task(
    name="notifications.queue_event_texts",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=5,
    ignore_result=True,
)
def queue_event_texts(kind, object_id):
    """Texts that go with a booking or payment email (the email is sent separately)."""
    events.queue_texts(kind, object_id)


@shared_task(name="notifications.run_reminders", ignore_result=True)
def run_reminders():
    """Queue the reminders due now. Every 15 minutes."""
    return reminders.run()


@shared_task(name="notifications.send_due_notifications", ignore_result=True)
def send_due_notifications():
    """Send texts held back by quiet hours, and retry lost ones. Every 5 minutes."""
    return dispatch.send_due()
