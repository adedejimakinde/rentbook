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


# Inline stroke icons: 24px grid, 2px stroke, round caps and joins (see DESIGN-SYSTEM.md).
ICONS = {
    "cart": '<path d="M4 5h2l2 10h10l2-7H7"/><circle cx="10" cy="19" r="1.5"/>'
    '<circle cx="17" cy="19" r="1.5"/>',
    "menu": '<path d="M4 8h16M4 16h16"/>',
    "close": '<path d="M6 6l12 12M18 6L6 18"/>',
    "chevron-right": '<path d="M9 6l6 6-6 6"/>',
    "chevron-left": '<path d="M15 6l-6 6 6 6"/>',
    "chevron-down": '<path d="M6 9l6 6 6-6"/>',
    "arrow-right": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "trash": '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
    "calendar": '<rect x="4" y="5" width="16" height="15" rx="2"/>'
    '<path d="M4 10h16M9 3v4M15 3v4"/>',
    "check-circle": '<circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/>',
    "check": '<path d="M5 12l5 5 9-10"/>',
    "shield": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3Z"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    "phone": '<path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2'
    'A16 16 0 0 1 3 6a2 2 0 0 1 2-2Z"/>',
    "mail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
    "pin": '<path d="M12 21s-7-6.2-7-12a7 7 0 0 1 14 0c0 5.8-7 12-7 12Z"/><circle cx="12" cy="9" '
    'r="2.5"/>',
    "truck": '<path d="M3 6h11v10H3zM14 10h4l3 3v3h-7"/><circle cx="7" cy="18" r="2"/>'
    '<circle cx="17" cy="18" r="2"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    "alert": '<circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16.5h.01"/>',
    "download": '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
    "edit": '<path d="M4 20h4L19 9l-4-4L4 16v4Z"/>',
    "instagram": '<rect x="4" y="4" width="16" height="16" rx="5"/>'
    '<circle cx="12" cy="12" r="3.5"/><path d="M16.5 7.5h.01"/>',
    "facebook": '<path d="M14 21v-8h3l.5-3.5H14V7.5c0-1 .4-1.7 1.8-1.7H18V2.7A20 20 0 0 0 15.4 2.5'
    'C12.8 2.5 11 4 11 6.9v2.6H8V13h3v8"/>',
    "tiktok": '<path d="M14 3v11.5a3.5 3.5 0 1 1-3.5-3.5M14 3c.4 2.6 2.2 4.4 5 4.6"/>',
}
FILLED = {
    "whatsapp": "M12.04 2a9.9 9.9 0 0 0-8.5 14.9L2 22l5.27-1.38A9.9 9.9 0 1 0 12.04 2Zm0 1.8a8.1 "
    "8.1 0 1 1-4.2 15.03l-.3-.18-3.13.82.84-3.05-.2-.31A8.1 8.1 0 0 1 12.04 3.8Zm-3.1 4.1c-.17 "
    "0-.45.06-.69.32-.24.26-.9.88-.9 2.15s.92 2.5 1.05 2.67c.13.17 1.8 2.86 4.45 3.9 2.2.87 2.65.7 "
    "3.13.65.48-.04 1.54-.63 1.76-1.24.22-.6.22-1.13.15-1.24-.06-.1-.24-.17-.5-.3-.26-.13-1.54-.76"
    "-1.78-.85-.24-.09-.41-.13-.59.13-.17.26-.67.85-.82 1.02-.15.18-.3.2-.57.07-.26-.13-1.1-.4-2.1"
    "-1.3-.77-.7-1.3-1.55-1.45-1.81-.15-.26-.02-.4.11-.53.12-.12.26-.3.4-.46.13-.15.17-.26.26-.44"
    ".09-.17.04-.33-.02-.46-.07-.13-.59-1.43-.81-1.96-.21-.51-.43-.44-.59-.45h-.5Z",
    "star": "M12 2.5l2.9 6 6.6.9-4.8 4.6 1.2 6.5L12 17.4l-5.9 3.1 1.2-6.5L2.5 9.4l6.6-.9L12 2.5Z",
    "sparkle": "M12 0c.7 7 5 11.3 12 12-7 .7-11.3 5-12 12-.7-7-5-11.3-12-12C7 11.3 11.3 7 12 0Z",
}


@register.simple_tag
def icon(name, size=20, css="", stroke=2):
    """Inline SVG icon, hidden from screen readers; it takes the colour of its text."""
    if name in FILLED:
        body = f'<path d="{FILLED[name]}"/>'
        attrs = 'fill="currentColor"'
    else:
        body = ICONS[name]
        attrs = (
            f'fill="none" stroke="currentColor" stroke-width="{stroke}" '
            'stroke-linecap="round" stroke-linejoin="round"'
        )
    css_attr = f' class="{css}"' if css else ""
    return mark_safe(  # noqa: S308 - fixed markup from the dicts above
        f'<svg{css_attr} width="{int(size)}" height="{int(size)}" viewBox="0 0 24 24" {attrs} '
        f'aria-hidden="true" focusable="false">{body}</svg>'
    )


@register.simple_tag
def stars(rating, size=16):
    """Gold stars for a 1-5 rating, with a text alternative."""
    rating = max(0, min(int(rating or 0), 5))
    filled = icon("star", size)
    empty = filled.replace('fill="currentColor"', 'fill="currentColor" opacity="0.25"')
    return mark_safe(  # noqa: S308 - built from fixed icon markup and an integer
        f'<span class="inline-flex gap-0.5 text-gold" role="img" '
        f'aria-label="{rating} out of 5 stars">{filled * rating}{empty * (5 - rating)}</span>'
    )


@register.filter
def nth(options, index):
    """Pick from a "|"-separated list by position, wrapping around: "a|b"|nth:3 -> "b"."""
    choices = [choice.strip() for choice in str(options).split("|")]
    return choices[int(index) % len(choices)]
