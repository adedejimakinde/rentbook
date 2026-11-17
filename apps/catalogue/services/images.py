"""Image processing: orientation, metadata stripping, downscaling and responsive renditions."""

import base64
import io
import posixpath

from django.core.files.base import ContentFile
from PIL import Image, ImageOps

WIDTHS = (400, 800, 1200)
ORIGINAL_MAX_SIDE = 2400
PLACEHOLDER_WIDTH = 16

# (format key, Pillow format, extension, MIME type, save options)
RENDITION_FORMATS = (
    ("avif", "AVIF", "avif", "image/avif", {"quality": 55, "speed": 6}),
    ("webp", "WEBP", "webp", "image/webp", {"quality": 78, "method": 4}),
    ("jpeg", "JPEG", "jpg", "image/jpeg", {"quality": 80, "optimize": True, "progressive": True}),
)
ORIGINAL_FORMATS = {
    "JPEG": ("jpg", {"quality": 90, "optimize": True}),
    "PNG": ("png", {"optimize": True}),
    "WEBP": ("webp", {"quality": 90, "method": 4}),
}


def _has_alpha(image):
    return image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)


def sanitise(image):
    """Return a fresh image with EXIF orientation applied and no metadata at all.

    Rebuilding from raw pixels drops EXIF, GPS, ICC profiles and text chunks, which
    Pillow would otherwise copy from ``image.info`` into some formats on save.
    """
    upright = ImageOps.exif_transpose(image)
    mode = "RGBA" if _has_alpha(upright) else "RGB"
    converted = upright.convert(mode)
    return Image.frombytes(mode, converted.size, converted.tobytes())


def _flatten(image):
    """RGB copy, with any transparency flattened onto white."""
    if image.mode == "RGB":
        return image
    background = Image.new("RGB", image.size, (255, 255, 255))
    background.paste(image, mask=image.getchannel("A"))
    return background


def _resize(image, width):
    height = max(1, round(image.height * width / image.width))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _downscale(image, max_side):
    longest = max(image.size)
    if longest <= max_side:
        return image
    scale = max_side / longest
    return image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )


def target_widths(original_width):
    """Widths to generate: the standard set that fits, never upscaling."""
    widths = {width for width in WIDTHS if width <= original_width}
    if original_width < WIDTHS[-1]:
        widths.add(original_width)
    return sorted(widths)


def _encode(image, pil_format, options):
    buffer = io.BytesIO()
    image.save(buffer, pil_format, **options)
    return buffer.getvalue()


def make_placeholder(image):
    """Tiny blurred stand-in as a base64 data URI, a few hundred bytes."""
    small = _resize(_flatten(image), min(PLACEHOLDER_WIDTH, image.width))
    data = _encode(small, "JPEG", {"quality": 40})
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def delete_renditions(instance):
    """Remove every generated file of an instance from storage."""
    storage = instance.image.storage
    for rendition in instance.renditions or []:
        storage.delete(rendition["name"])


def process_image(instance):
    """Clean the stored original and generate all renditions for an ItemImage or Category.

    Applies EXIF orientation, strips all metadata, downscales the original to at most
    ORIGINAL_MAX_SIDE on its long side, then writes AVIF, WebP and JPEG files at each
    target width. Records sizes and a placeholder on the row. Safe to run twice.
    """
    if not instance.image:
        return
    storage = instance.image.storage
    with instance.image.open("rb") as handle:
        source = Image.open(handle)
        source_format = source.format
        source.load()

    original = _downscale(sanitise(source), ORIGINAL_MAX_SIDE)
    out_format = source_format if source_format in ORIGINAL_FORMATS else "JPEG"
    extension, options = ORIGINAL_FORMATS[out_format]
    if out_format == "JPEG":
        original = _flatten(original)

    old_name = instance.image.name
    stem, _ = posixpath.splitext(old_name)
    new_name = f"{stem}.{extension}"
    storage.delete(old_name)
    if new_name != old_name:
        storage.delete(new_name)
    saved_name = storage.save(new_name, ContentFile(_encode(original, out_format, options)))

    delete_renditions(instance)
    model_name = instance._meta.model_name
    folder = f"renditions/{model_name}/{instance.pk}"
    base_name = posixpath.basename(stem)
    flat = _flatten(original)
    renditions = []
    for width in target_widths(original.width):
        resized = flat if width == flat.width else _resize(flat, width)
        for key, pil_format, extension, _mime, save_options in RENDITION_FORMATS:
            name = storage.save(
                f"{folder}/{base_name}-{width}.{extension}",
                ContentFile(_encode(resized, pil_format, save_options)),
            )
            renditions.append(
                {"format": key, "width": resized.width, "height": resized.height, "name": name}
            )

    type(instance).objects.filter(pk=instance.pk).update(
        image=saved_name,
        image_width=original.width,
        image_height=original.height,
        placeholder=make_placeholder(flat),
        renditions=renditions,
        renditions_ready=True,
    )
