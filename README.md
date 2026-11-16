# Rentbook

Booking, payment and invoicing platform for Atglace Events & Rentals LLC (DFW area).
Stack: Django 5.2, HTMX, Alpine.js, Tailwind (django-tailwind-cli), django-unfold admin,
PostgreSQL 16, Celery + Redis. See `CLAUDE.md` for project rules.

## Setup with Docker

```bash
cp .env.example .env
```

Edit `.env`: set `SECRET_KEY` and a private `ADMIN_URL` (it must not be `admin/`).
`.env.example` is set up for production (`config.settings.prod`, `DEBUG=False`); the
development `docker-compose.yml` overrides both to the dev settings for you.

```bash
docker compose up -d
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py tailwind build
```

- Site: http://localhost:8000/
- Health check: http://localhost:8000/health/
- Admin: http://localhost:8000/ plus the value of `ADMIN_URL`

While editing templates, rebuild CSS automatically:

```bash
docker compose exec web python manage.py tailwind watch
```

## Tests and linting

```bash
docker compose exec web pytest
docker compose exec web ruff check .
docker compose exec web ruff format .
```

## Setup without Docker

Requires Python 3.12+, PostgreSQL 16 and Redis running locally.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

In `.env`, point `DATABASE_URL` and `REDIS_URL` at `localhost` instead of `db` and `redis`,
and set `DJANGO_SETTINGS_MODULE=config.settings.dev` and `DEBUG=True`.

```bash
python manage.py migrate
python manage.py tailwind build
python manage.py runserver
celery -A config worker --loglevel=info
celery -A config beat --loglevel=info
```

## Settings

`config/settings/base.py` holds shared settings, `dev.py` is for local work and `prod.py`
turns on HTTPS redirect, secure cookies, HSTS and hashed static files. Select one with
`DJANGO_SETTINGS_MODULE` (`manage.py` defaults to dev, `wsgi.py` and `asgi.py` to prod).
Everything configurable comes from environment variables, listed in `.env.example`.
The dev settings refuse to start when `DEBUG` is off, so they can never serve production.

## Production

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml exec web python manage.py migrate
```

This runs gunicorn (behind Cloudflare and the VPS's reverse proxy on port 8000), the
Celery worker and Beat, with `config.settings.prod` and `requirements.txt` only.

## Front end assets

HTMX and Alpine.js are vendored in `static/js/` and Montserrat (500 and 700, Latin subset)
in `static/fonts/`, so no third-party hosts are contacted. Tailwind source is
`assets/css/source.css` and the built `static/css/tailwind.css` is not committed.
