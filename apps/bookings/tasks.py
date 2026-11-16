"""Background jobs for bookings."""

from celery import shared_task

from apps.bookings.services import availability


@shared_task(name="bookings.release_expired_holds", ignore_result=True)
def release_expired_holds():
    """Release stock from holds that have lapsed. Runs every minute from Celery Beat."""
    return availability.release_expired_holds()
