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


@pytest.fixture(autouse=True)
def queued_tasks(monkeypatch):
    """Keep the Celery jobs the services queue after commit, without running them.

    Tests that want documents issued and emails sent use run_tasks instead.
    """
    from apps.core import queue

    queued = []
    monkeypatch.setattr(queue, "enqueue", lambda task, *args: queued.append((task.name, args)))
    return queued


@pytest.fixture
def run_tasks(monkeypatch):
    """Run each queued Celery job at once, in the test, as a worker would."""
    from apps.core import queue

    ran = []

    def run(task, *args):
        ran.append((task.name, args))
        task.apply(args=args, throw=True)

    monkeypatch.setattr(queue, "enqueue", run)
    return ran
