"""Fixtures for catalogue tests: isolated media storage and generated photos."""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    """Write uploads and renditions to a throwaway directory."""
    settings.MEDIA_ROOT = tmp_path / "media"
    return settings.MEDIA_ROOT


def make_jpeg(size=(3000, 1500), orientation=None, gps=False):
    """JPEG bytes, optionally carrying an EXIF orientation, camera make and GPS position."""
    image = Image.new("RGB", size, (200, 30, 90))
    exif = Image.Exif()
    exif[0x010F] = "TestCam"
    if orientation:
        exif[0x0112] = orientation
    if gps:
        exif[0x8825] = {1: "N", 2: (32.0, 47.0, 0.0), 3: "W", 4: (96.0, 48.0, 0.0)}
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)
    return buffer.getvalue()


@pytest.fixture
def upload():
    """Factory for an uploaded photo file."""

    def build(name="photo.jpg", **kwargs):
        return SimpleUploadedFile(name, make_jpeg(**kwargs), content_type="image/jpeg")

    return build
