"""Local development settings. Insecure fallbacks exist here only, never in prod."""

import os

from django.core.exceptions import ImproperlyConfigured

os.environ.setdefault("SECRET_KEY", "dev-only-insecure-key")
os.environ.setdefault("ADMIN_URL", "dev-admin/")
os.environ.setdefault("DEBUG", "True")

from .base import *  # noqa: E402, F403
from .base import DEBUG, env  # noqa: E402

if not DEBUG:
    raise ImproperlyConfigured(
        "config.settings.dev was loaded with DEBUG off, which means it is being used to "
        "serve real traffic. Set DJANGO_SETTINGS_MODULE=config.settings.prod in production."
    )

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "testserver"]

# Extra hosts and origins for demos, for example a tunnel or GitHub Codespaces. Dev only.
ALLOWED_HOSTS += [host.strip() for host in env.list("DEV_EXTRA_HOSTS", default=[]) if host.strip()]
CSRF_TRUSTED_ORIGINS = [
    origin.strip() for origin in env.list("DEV_CSRF_ORIGINS", default=[]) if origin.strip()
]

if env.bool("CODESPACES", default=False):
    ALLOWED_HOSTS.append(".app.github.dev")
    CSRF_TRUSTED_ORIGINS.append("https://*.app.github.dev")

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
