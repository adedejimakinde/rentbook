"""Template tags for SEO markup."""

import json

from django import template
from django.conf import settings
from django.utils.safestring import mark_safe

register = template.Library()


@register.simple_tag
def local_business_jsonld():
    """LocalBusiness structured data as a JSON-LD script tag, serving the DFW metroplex."""
    data = {
        "@context": "https://schema.org",
        "@type": "LocalBusiness",
        "name": settings.BUSINESS_NAME,
        "description": f"{settings.BUSINESS_TAGLINE}. Chairs, tables, linen, bounce houses, "
        "backdrops, a golf simulator and concession machines, delivered and set up.",
        "url": settings.SITE_URL,
        "telephone": settings.BUSINESS_PHONE,
        "email": settings.BUSINESS_EMAIL,
        "priceRange": "$$",
        "areaServed": {
            "@type": "AdministrativeArea",
            "name": "Dallas-Fort Worth metroplex, Texas",
        },
        "makesOffer": {
            "@type": "Offer",
            "itemOffered": {"@type": "Service", "name": "Event and party equipment rental"},
        },
    }
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    return mark_safe(f'<script type="application/ld+json">{payload}</script>')  # noqa: S308


@register.simple_tag
def og_image_url(image):
    """Absolute URL of the largest JPEG rendition of an image, or "" if none exists yet."""
    if not getattr(image, "renditions_ready", False):
        return ""
    jpegs = [r for r in image.renditions if r["format"] == "jpeg"]
    if not jpegs:
        return ""
    best = max(jpegs, key=lambda r: r["width"])
    return settings.SITE_URL + image.image.storage.url(best["name"])
