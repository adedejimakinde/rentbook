"""Background jobs for payments."""

from celery import shared_task

from apps.payments.services import stripe as stripe_service


@shared_task(name="payments.sync_stripe", ignore_result=True)
def sync_stripe():
    """Match kept unmatched Stripe events and settle stale pending payments and refunds.

    Runs every 15 minutes from Celery Beat.
    """
    return {
        "unmatched_resolved": stripe_service.retry_unmatched(),
        "bookings_synced": stripe_service.sync_stale_bookings(),
    }
