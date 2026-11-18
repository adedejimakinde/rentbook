"""Template context shared by every page."""

import re

from django.conf import settings

from apps.core.services import SERVICE_CITIES


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
        "service_cities": SERVICE_CITIES,
        "social_links": [
            (name, url)
            for name, url in (
                ("Instagram", settings.SOCIAL_INSTAGRAM_URL),
                ("TikTok", settings.SOCIAL_TIKTOK_URL),
                ("Facebook", settings.SOCIAL_FACEBOOK_URL),
            )
            if url
        ],
    }


def site_sections(request):
    """Which optional sections (packages, gallery) have content to link to.

    Staff screens do not show the public navigation, so they skip the queries.
    """
    match = getattr(request, "resolver_match", None)
    if match and match.namespace in ("admin", "dashboard"):
        return {}
    from apps.core.services import section_flags

    return section_flags()
