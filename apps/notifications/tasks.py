"""Background job that sends one email, retried with backoff if the mail service fails."""

from celery import shared_task

from apps.notifications.services import emails


@shared_task(
    name="notifications.send_email",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
    ignore_result=True,
)
def send_email(kind, object_id):
    """Build and send one email of kind for object_id, unless it was already sent."""
    emails.send(kind, object_id)
