"""Template tag that renders responsive <picture> markup from stored renditions."""

from django import template
from django.utils.html import format_html, format_html_join

register = template.Library()

DEFAULT_SIZES = "(min-width: 1024px) 400px, (min-width: 640px) 50vw, 100vw"
MIME = {"avif": "image/avif", "webp": "image/webp", "jpeg": "image/jpeg"}


def _srcset(storage, renditions, key):
    return ", ".join(
        f"{storage.url(r['name'])} {r['width']}w"
        for r in sorted(renditions, key=lambda r: r["width"])
        if r["format"] == key
    )


@register.simple_tag
def picture(image, name="", alt="", sizes=DEFAULT_SIZES, eager=False, ratio="4/3", css=""):
    """Render an ItemImage or Category image as <picture>, or a placeholder box.

    Until the Celery task has produced renditions (``renditions_ready``) a light purple
    box with ``name`` is shown, at the same aspect ratio, so the layout never jumps.
    Below-the-fold images are lazy; pass ``eager=True`` for the first screen only.
    """
    box = f"relative block overflow-hidden bg-[#e9d8fb] {css}".strip()
    style = f"aspect-ratio:{ratio}"
    if image is None or not image.renditions_ready or not image.renditions:
        return format_html(
            '<span class="{} flex items-center justify-center p-3 text-center font-bold '
            'text-primary" style="{}" aria-hidden="true">{}</span>',
            box,
            style,
            name,
        )
    storage = image.image.storage
    fallback = sorted(
        (r for r in image.renditions if r["format"] == "jpeg"), key=lambda r: r["width"]
    )
    default = min(fallback, key=lambda r: abs(r["width"] - 800))
    sources = format_html_join(
        "",
        '<source type="{}" srcset="{}" sizes="{}">',
        (
            (MIME[key], _srcset(storage, image.renditions, key), sizes)
            for key in ("avif", "webp")
            if any(r["format"] == key for r in image.renditions)
        ),
    )
    alt_text = alt or getattr(image, "alt_text", "") or name
    return format_html(
        '<span class="{}" style="{}">'
        '<span class="absolute inset-0 scale-110 blur-xl" aria-hidden="true" '
        "style=\"background:url('{}') center/cover\"></span>"
        "<picture>{}"
        '<img src="{}" srcset="{}" sizes="{}" width="{}" height="{}" alt="{}" '
        'loading="{}" decoding="async" class="absolute inset-0 h-full w-full object-cover">'
        "</picture></span>",
        box,
        style,
        image.placeholder,
        sources,
        storage.url(default["name"]),
        _srcset(storage, image.renditions, "jpeg"),
        sizes,
        default["width"],
        default["height"],
        alt_text,
        "eager" if eager else "lazy",
    )


@register.filter
def get_item(mapping, key):
    """Look up a key in a dict from a template, tolerating a missing dict."""
    return mapping.get(key) if mapping else None
