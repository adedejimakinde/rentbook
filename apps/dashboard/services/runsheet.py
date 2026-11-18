"""The printable run sheet: the crew schedule for a day as a PDF, with a checklist per stop."""

from django.conf import settings
from django.template.loader import render_to_string

from apps.dashboard.services import crew
from apps.dashboard.services.format import dollars

DELIVERY_CHECKLIST = [
    "Check the items and counts against this sheet",
    "Set up on the surface listed, clear of hazards",
    "Stake or sandbag anything inflatable",
    "Plug in power equipment and test it",
    "Photograph the finished setup",
    "Walk the customer through safe use",
]
PICKUP_CHECKLIST = [
    "Count every item back onto the truck",
    "Inspect each item for damage and photograph any",
    "Wipe down and bag linens and soft items",
    "Check the site is left clean",
]


def checklist_for(card):
    """Setup or pickup steps for one stop, with only the lines this viewer may see."""
    steps = list(DELIVERY_CHECKLIST if card.stop.is_delivery else PICKUP_CHECKLIST)
    if not card.power_items and card.stop.is_delivery:
        steps.remove("Plug in power equipment and test it")
    if card.show_money and card.balance:
        steps.append(f"Collect {dollars(card.balance)} before leaving")
    if card.show_money and not card.stop.is_delivery and card.caution_held:
        steps.append(
            f"Caution deposit {dollars(card.caution_held)}: refund only after the inspection"
        )
    return steps


def render_html(day, user):
    """The run sheet as HTML, built for what this user may see."""
    cards = crew.schedule(day, user)
    for card in cards:
        card.checklist = checklist_for(card)
    return render_to_string(
        "dashboard/run_sheet.html",
        {
            "day": day,
            "cards": cards,
            "business_name": settings.BUSINESS_NAME,
            "business_phone": settings.BUSINESS_PHONE,
        },
    )


def render_pdf(day, user):
    """The run sheet for day as PDF bytes."""
    from weasyprint import HTML

    html = render_html(day, user)
    return HTML(string=html, base_url=str(settings.BASE_DIR)).write_pdf()
