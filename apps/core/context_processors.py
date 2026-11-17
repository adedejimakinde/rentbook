"""Template context shared by every page."""

import re

from django.conf import settings


def business(request):
    """Expose business name, contact details and WhatsApp number from settings."""
    phone_digits = re.sub(r"\D", "", settings.BUSINESS_PHONE)
    return {
        "business_name": settings.BUSINESS_NAME,
        "business_tagline": settings.BUSINESS_TAGLINE,
        "business_area": settings.BUSINESS_AREA,
        "business_phone": settings.BUSINESS_PHONE,
        "business_phone_href": f"+1{phone_digits[-10:]}",
        "business_email": settings.BUSINESS_EMAIL,
        "whatsapp_number": settings.WHATSAPP_NUMBER,
        "site_url": settings.SITE_URL,
    }


def site_sections(request):
    """Which optional sections (packages, gallery) have content to link to."""
    from apps.core.services import section_flags

    return section_flags()
