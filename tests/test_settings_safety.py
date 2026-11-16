"""Production must never run on the development settings."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def start_django(settings_module, **env):
    """Import settings and set Django up in a fresh interpreter; return the process result."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("DEBUG", "DJANGO_SETTINGS_MODULE")
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
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", "import django; django.setup()"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_dev_settings_refuse_to_start_with_debug_off():
    result = start_django("config.settings.dev", DEBUG="False")
    assert result.returncode != 0
    assert "config.settings.prod" in result.stderr


def test_dev_settings_start_with_debug_on():
    assert start_django("config.settings.dev").returncode == 0
    assert start_django("config.settings.dev", DEBUG="True").returncode == 0


def test_prod_settings_start():
    assert start_django("config.settings.prod").returncode == 0


def env_example():
    lines = (ROOT / ".env.example").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


def test_env_example_uses_production_settings():
    values = env_example()
    assert values["DJANGO_SETTINGS_MODULE"] == "config.settings.prod"
    assert values["DEBUG"] == "False"
    text = (ROOT / ".env.example").read_text()
    assert "config.settings.dev" in text


def test_production_compose_runs_gunicorn_with_production_requirements():
    compose = (ROOT / "docker-compose.prod.yml").read_text()
    assert "gunicorn config.wsgi:application" in compose
    assert "runserver" not in compose
    assert "REQUIREMENTS: requirements.txt" in compose
    assert "requirements-dev" not in compose
    assert ".:/app" not in compose
    assert "DJANGO_SETTINGS_MODULE: config.settings.prod" in compose
    for service in ("web:", "worker:", "beat:", "db:", "redis:"):
        assert service in compose


def test_dockerfile_installs_the_requirements_it_is_given():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "ARG REQUIREMENTS=requirements-dev.txt" in dockerfile
    assert 'pip install --no-cache-dir -r "$REQUIREMENTS"' in dockerfile
