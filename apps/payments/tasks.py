"""Background jobs for payments. Each runs on its own Celery Beat schedule."""

from celery import shared_task

from apps.payments.services import stripe as stripe_service


@shared_task(name="payments.retry_unmatched_stripe_events", ignore_result=True)
def retry_unmatched_stripe_events():
    """Match kept Stripe events that matched no payment when they arrived. Every 15 minutes."""
    return stripe_service.retry_unmatched()


@shared_task(name="payments.sync_stale_payments", ignore_result=True)
def sync_stale_payments():
    """Settle payments and refunds left pending, from what Stripe holds. Every 15 minutes."""
    return stripe_service.sync_stale_bookings()


@shared_task(name="payments.sync_recent_paid_bookings", ignore_result=True)
def sync_recent_paid_bookings():
    """Sync Stripe-paid bookings changed lately or with an event soon. Hourly."""
    return stripe_service.sync_recent_paid_bookings()
