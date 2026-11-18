"""The three staff groups and what each may do.

Owner has every permission. Office runs bookings, customers and recording
payments, but cannot issue refunds or touch site settings. Crew see the crew
schedule, mark bookings delivered or returned from it, and have read-only
bookings with no payments or refunds.
"""

OWNER = "Owner"
OFFICE = "Office"
CREW = "Crew"

BOOKING_MODELS_EDIT = ("booking", "bookingitem", "bookingpackage")
VIEW_ONLY = {
    "bookings": ("deliveryzone", "promocode"),
    "catalogue": ("category", "item", "itemvariant", "package", "packageitem"),
    "inventory": ("stockadjustment",),
    "notifications": ("sentemail", "notification", "smsoptout"),
    "payments": ("payment",),
    "invoices": ("invoice",),
}

OFFICE_PERMISSIONS = [
    *(
        ("bookings", f"{action}_{model}")
        for model in BOOKING_MODELS_EDIT
        for action in ("view", "add", "change")
    ),
    ("bookings", "delete_bookingitem"),
    ("bookings", "delete_bookingpackage"),
    ("bookings", "view_dashboard"),
    ("bookings", "view_crew_schedule"),
    ("invoices", "add_invoice"),
    *((app, f"view_{model}") for app, models in VIEW_ONLY.items() for model in models),
]

CREW_PERMISSIONS = [
    ("bookings", "view_booking"),
    ("bookings", "view_bookingitem"),
    ("bookings", "view_bookingpackage"),
    ("bookings", "view_crew_schedule"),
]
