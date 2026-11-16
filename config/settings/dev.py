"""Local development settings. Insecure fallbacks exist here only, never in prod."""

import os

os.environ.setdefault("SECRET_KEY", "dev-only-insecure-key")
os.environ.setdefault("ADMIN_URL", "dev-admin/")
os.environ.setdefault("DEBUG", "True")

from .base import *  # noqa: E402, F403

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "testserver"]
