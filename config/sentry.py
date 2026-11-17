"""Error reporting to Sentry, switched on only when SENTRY_DSN is set."""

import logging

import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration


def init_sentry(dsn, *, environment="production", traces_sample_rate=0.0):
    """Start Sentry with the Django, Celery and logging integrations; return whether it started.

    Log records at ERROR and above (every logger.exception) become Sentry
    events; INFO and above are kept as breadcrumbs. No personal data is sent.
    """
    if not dsn:
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        integrations=[
            DjangoIntegration(),
            CeleryIntegration(),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        send_default_pii=False,
        traces_sample_rate=traces_sample_rate,
    )
    return True
