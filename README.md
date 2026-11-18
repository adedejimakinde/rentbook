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

This runs gunicorn, the Celery worker and Beat, with `config.settings.prod` and
`requirements.txt` only. The Tailwind CSS is built into the image at `docker build` time
(so containers start without a build step or network access); `collectstatic` runs when
the web container starts. `.dockerignore` keeps `.env`, `.git` and local caches out of
the image: settings and secrets come from `.env` at run time.

Set `SENTRY_DSN` in `.env` to report errors to Sentry (Django, Celery and every
`logger.exception`); leave it empty to switch Sentry off.

### Reverse proxy (Caddy)

gunicorn listens on `127.0.0.1:8000` only, so it is never reachable from outside the VPS.
Put Caddy on the VPS in front of it to terminate TLS. Traffic then runs Cloudflare →
Caddy (443) → gunicorn (127.0.0.1:8000).

1. Install Caddy on the VPS (`apt install caddy` from the official Caddy repository).
2. Write `/etc/caddy/Caddyfile`, with your domain:

   ```caddyfile
   example.com, www.example.com {
       encode zstd gzip
       reverse_proxy 127.0.0.1:8000 {
           header_up X-Forwarded-Proto {scheme}
       }
       request_body {
           max_size 10MB
       }
   }
   ```

3. `sudo systemctl reload caddy`. Caddy gets and renews the certificate itself.
4. In Cloudflare, set SSL/TLS to **Full (strict)** so the Cloudflare → Caddy leg is
   encrypted and checked too. If the origin certificate cannot be issued over HTTP
   (for example the proxy hides port 80), use a Cloudflare origin certificate in a
   `tls` line instead.
5. Open only ports 80 and 443 on the VPS firewall (plus SSH); never 8000.

Django trusts `X-Forwarded-Proto` (`SECURE_PROXY_SSL_HEADER` in `prod.py`), which is why
port 8000 must stay bound to localhost: only Caddy may set that header. Static files are
served by WhiteNoise from gunicorn with long cache headers, so Caddy needs no static
file rules.

## Staff screens and roles

The admin home (`ADMIN_URL`) is the owner dashboard. Next to it, under the same URL:
`calendar/` (month and week), `crew/` (today's crew schedule, `crew/<YYYY-MM-DD>/` for a
day) and `crew/<date>/run-sheet.pdf`. They are read-only views; "Mark delivered" and
"Mark returned" call the booking lifecycle services, with the same override-note rules as the
admin. On phones a bottom tab bar (Home, Calendar, Bookings, Crew, Payments) replaces the
desktop sidebar.

Three groups are created by a data migration (`apps/dashboard/migrations/0001_staff_groups.py`;
the lists are in `apps/dashboard/roles.py`). Give a person the group, and tick **Staff status**
on their user so they can sign in.

| | Owner | Office | Crew |
|---|---|---|---|
| Dashboard, calendar, quick switches | yes | dashboard and calendar; no switches | no (opening the admin home goes to the crew schedule) |
| Bookings | everything | view, add and change, including lines | read-only, no prices, no payments |
| Customers | everything | yes (they are on the booking) | phone only if the switch allows it |
| Recording payments (Zelle, Cash App, bank) | yes | yes | no |
| Refunds, resolving mismatched payments | yes | **no** | no |
| Tax exemption, site settings | yes | **no** | no |
| Crew schedule, run sheet, mark delivered or returned | yes | yes | **yes** |
| Catalogue, inventory, invoices, notifications | everything | view (office can also issue invoices) | no |

Owner always holds every permission, including ones added later (it is topped up after each
`migrate`). Run `manage.py migrate dashboard zero` then `migrate` to reset the groups to these
defaults.

Two site-setting switches control what Crew see: **Crew sees customer phone** (default on) and
**Crew sees money** (default off: balance to collect and caution held). They apply to the crew
schedule, the run sheet and the booking detail page. Owner and Office always see both. The
dashboard's Quick switches card also holds Online booking, Text reminders and Wind alerts (wind
alerts are a setting only for now). Every change is written to the site settings history with
who made it and when.

Delivery and pickup times live on the booking: **Delivery time**, **Pickup date** (empty means
the event date) and **Pickup time**. A stop with no time shows the setup-by time, or "Time TBC",
and is listed last.

## Front end assets

HTMX and Alpine.js are vendored in `static/js/` and Montserrat (500 and 700, Latin subset)
in `static/fonts/`, so no third-party hosts are contacted. Tailwind source is
`assets/css/source.css` and the built `static/css/tailwind.css` is not committed.
