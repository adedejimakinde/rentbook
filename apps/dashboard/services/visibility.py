"""What each kind of staff member may see on the crew schedule and run sheet.

Owner and Office always see phone numbers and money. Crew see them only while
the owner's switches allow it.
"""

from dataclasses import dataclass

from apps.core import site_settings

SEES_PHONE_PERMISSION = "bookings.change_booking"
SEES_MONEY_PERMISSION = "payments.view_payment"


@dataclass(frozen=True)
class Visibility:
    show_phone: bool
    show_money: bool


def visibility_for(user):
    """Phone and money visibility for user, from their permissions and the owner's switches."""
    row = site_settings.get()
    return Visibility(
        show_phone=user.has_perm(SEES_PHONE_PERMISSION) or row.crew_sees_customer_phone,
        show_money=user.has_perm(SEES_MONEY_PERMISSION) or row.crew_sees_money,
    )
