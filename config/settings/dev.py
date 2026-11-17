"""Local development settings. Insecure fallbacks exist here only, never in prod."""

import os

from django.core.exceptions import ImproperlyConfigured

os.environ.setdefault("SECRET_KEY", "dev-only-insecure-key")
os.environ.setdefault("ADMIN_URL", "dev-admin/")
os.environ.setdefault("DEBUG", "True")

from .base import *  # noqa: E402, F403
from .base import DEBUG  # noqa: E402

if not DEBUG:
    raise ImproperlyConfigured(
        "config.settings.dev was loaded with DEBUG off, which means it is being used to "
        "serve real traffic. Set DJANGO_SETTINGS_MODULE=config.settings.prod in production."
    )

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "testserver"]

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
