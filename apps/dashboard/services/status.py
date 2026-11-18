"""Booking status as a text label and a colour tone, shared by every staff screen.

Status is never shown by colour alone: each tone always comes with its label.
"""

from apps.bookings.models import Booking

# status -> (label, tone). Tones are the design system's: brand, success, warning, danger, muted.
STATUS_STYLES = {
    Booking.Status.ENQUIRY: ("Enquiry", "muted"),
    Booking.Status.QUOTED: ("Quoted", "muted"),
    Booking.Status.HELD: ("Held, not paid", "warning"),
    Booking.Status.DEPOSIT_PAID: ("Deposit paid", "brand"),
    Booking.Status.PAID: ("Paid in full", "success"),
    Booking.Status.DELIVERED: ("Delivered", "brand"),
    Booking.Status.RETURNED: ("Returned", "muted"),
    Booking.Status.CANCELLED: ("Cancelled", "danger"),
    Booking.Status.EXPIRED: ("Expired", "muted"),
}

# Statuses shown on the calendar and in "Next 7 days": bookings that are happening.
SCHEDULED_STATUSES = (
    Booking.Status.HELD,
    Booking.Status.DEPOSIT_PAID,
    Booking.Status.PAID,
    Booking.Status.DELIVERED,
    Booking.Status.RETURNED,
)


def status_style(status):
    """(label, tone) for a booking status."""
    return STATUS_STYLES[status]
