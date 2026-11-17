"""Fixtures shared by every app's tests."""

from datetime import timedelta

import pytest
from django.utils import timezone


@pytest.fixture
def event_date():
    """A bookable date well past the minimum notice."""
    return timezone.localdate() + timedelta(days=60)


@pytest.fixture(autouse=True)
def local_cache(settings):
    """Give each test its own empty in-memory cache instead of the shared Redis one."""
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()
