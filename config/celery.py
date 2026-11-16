"""Celery application for background and scheduled jobs."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("rentbook")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Print the request of this task; used to confirm the worker is wired up."""
    print(f"Request: {self.request!r}")
