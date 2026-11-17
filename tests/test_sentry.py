"""Sentry starts only when a DSN is configured, with the Django, Celery and logging integrations."""

import logging
from unittest import mock

from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

from config import sentry


def test_nothing_is_started_without_a_dsn():
    with mock.patch.object(sentry.sentry_sdk, "init") as init:
        assert sentry.init_sentry("") is False
    init.assert_not_called()


def test_a_dsn_starts_sentry_with_the_integrations():
    with mock.patch.object(sentry.sentry_sdk, "init") as init:
        assert sentry.init_sentry("https://key@o0.ingest.sentry.io/1", environment="test")

    kwargs = init.call_args.kwargs
    assert kwargs["dsn"] == "https://key@o0.ingest.sentry.io/1"
    assert kwargs["environment"] == "test"
    assert kwargs["send_default_pii"] is False
    types = {type(integration) for integration in kwargs["integrations"]}
    assert {DjangoIntegration, CeleryIntegration, LoggingIntegration} <= types
    logging_integration = next(
        i for i in kwargs["integrations"] if isinstance(i, LoggingIntegration)
    )
    assert logging_integration._handler.level == logging.ERROR
