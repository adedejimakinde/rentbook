"""Codespaces and tunnel hosts are allowed by the dev settings only, never by prod."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOW = (
    "import json, django; django.setup(); from django.conf import settings; "
    "print(json.dumps([settings.ALLOWED_HOSTS, settings.CSRF_TRUSTED_ORIGINS]))"
)


def read_settings(settings_module, **env):
    """Return (ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS) from a fresh interpreter."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("DEBUG", "DJANGO_SETTINGS_MODULE", "CODESPACES")
        and not key.startswith(("DEV_", "ALLOWED_HOSTS", "CSRF_TRUSTED"))
    }
    environment.update(
        {
            "DJANGO_SETTINGS_MODULE": settings_module,
            "SECRET_KEY": "test-only",
            "ADMIN_URL": "private-admin/",
            "ALLOWED_HOSTS": "example.com",
            **env,
        }
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", SHOW],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.splitlines()[-1])


def test_dev_allows_only_local_hosts_by_default():
    hosts, origins = read_settings("config.settings.dev")
    assert hosts == ["localhost", "127.0.0.1", "[::1]", "testserver"]
    assert origins == []


def test_dev_extra_hosts_and_origins_come_from_the_environment():
    hosts, origins = read_settings(
        "config.settings.dev",
        DEV_EXTRA_HOSTS="demo.example.com, other.example.com",
        DEV_CSRF_ORIGINS="https://demo.example.com,https://other.example.com",
    )
    assert {"demo.example.com", "other.example.com", "localhost"} <= set(hosts)
    assert origins == ["https://demo.example.com", "https://other.example.com"]


def test_codespaces_allows_and_trusts_github_forwarded_domains():
    hosts, origins = read_settings("config.settings.dev", CODESPACES="true")
    assert ".app.github.dev" in hosts
    assert "https://*.app.github.dev" in origins


def test_codespaces_keeps_custom_hosts_too():
    hosts, origins = read_settings(
        "config.settings.dev",
        CODESPACES="true",
        DEV_EXTRA_HOSTS="demo.example.com",
        DEV_CSRF_ORIGINS="https://demo.example.com",
    )
    assert {".app.github.dev", "demo.example.com"} <= set(hosts)
    assert origins == ["https://demo.example.com", "https://*.app.github.dev"]


def test_prod_ignores_the_dev_variables_and_codespaces():
    hosts, origins = read_settings(
        "config.settings.prod",
        CODESPACES="true",
        DEV_EXTRA_HOSTS="demo.example.com",
        DEV_CSRF_ORIGINS="https://demo.example.com",
    )
    assert hosts == ["example.com"]
    assert not any("github.dev" in origin or "demo.example.com" in origin for origin in origins)
