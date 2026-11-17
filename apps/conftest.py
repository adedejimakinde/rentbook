"""Fixtures shared by every app's tests."""

from datetime import timedelta

import pytest
from django.utils import timezone


@pytest.fixture
def event_date():
    """A bookable date well past the minimum notice."""
    return timezone.localdate() + timedelta(days=60)
