"""The Quick switches card: the owner's most-used site settings."""

from dataclasses import dataclass

from apps.core import site_settings

CHANGE_PERMISSION = "core.change_sitesettings"
VIEW_PERMISSION = "core.view_sitesettings"


@dataclass
class SwitchRow:
    name: str
    label: str
    hint: str
    on: bool
    note: str = ""


def rows(row=None):
    """The five switches in the order the owner sees them, with their current state."""
    row = row or site_settings.get()
    sms = site_settings.sms(row)
    return [
        SwitchRow(
            "online_booking_enabled",
            "Online booking",
            "Customers can book and pay on the website",
            row.online_booking_enabled,
        ),
        SwitchRow(
            "crew_sees_customer_phone",
            "Crew sees customer phone",
            "Tap-to-call on the crew schedule",
            row.crew_sees_customer_phone,
        ),
        SwitchRow(
            "crew_sees_money",
            "Crew sees money",
            "Caution amounts and balances to collect",
            row.crew_sees_money,
        ),
        SwitchRow(
            "sms_enabled",
            "Text reminders",
            "Payment and pre-event texts to customers",
            row.sms_enabled,
            note="" if sms.configured else "Texting is not set up yet, so none are sent.",
        ),
        SwitchRow(
            "wind_alerts_enabled",
            "Wind alerts",
            "Warn me when inflatables face high wind",
            row.wind_alerts_enabled,
        ),
    ]


def can_view(user):
    return user.has_perm(VIEW_PERMISSION) or user.has_perm(CHANGE_PERMISSION)


def can_change(user):
    return user.has_perm(CHANGE_PERMISSION)
