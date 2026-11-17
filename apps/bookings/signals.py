"""Signal receivers for bookings."""

from django.db.models.signals import pre_delete
from django.dispatch import receiver

from apps.bookings.models import Booking, check_deletable


@receiver(pre_delete, sender=Booking, dispatch_uid="refuse_deleting_live_bookings")
def refuse_deleting_live_bookings(sender, instance, **kwargs):
    """Backstop for deletes that bypass Booking.delete and the queryset (such as cascades).

    Booking.delete and the queryset check first, outside the delete's
    transaction; this catches anything else. The refusal is a ProtectedError
    (BookingDeleteRefused), and the booking admin turns it into an error
    message rather than a server error.
    """
    check_deletable(instance)
