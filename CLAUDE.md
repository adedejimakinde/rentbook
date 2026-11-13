# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

A booking, payment and invoicing platform for a US event rentals business. The business rents chairs, tables, linen, bounce houses, backdrops, a golf simulator, and popcorn and cotton candy machines for parties, weddings, corporate, school and church events.

Customers browse the catalogue, pick an event date, location and items, see live availability, get an instant quote (including delivery and setup fees by zone), pay a deposit or the full amount, and receive an invoice automatically. Staff manage bookings, inventory, payments and deliveries from an admin dashboard.

Users: customers (individuals, event planners, churches, organisations), the owner/admin, and logistics staff.

Currency is USD. Time zone is the business's US time zone (set in settings as `TIME_ZONE`, not hard-coded elsewhere).

## Stack

- Python 3.12, Django 5.2 LTS
- HTMX for server-driven interactivity, Alpine.js for small client-side behaviour only
- Tailwind CSS via `django-tailwind-cli` (standalone binary, no Node.js)
- Django Admin with the `django-unfold` theme for the back office
- PostgreSQL 16
- Celery with Redis for background jobs (emails, PDFs, reminders); Celery Beat for scheduled jobs
- Stripe (Checkout + webhooks) for cards and ACH
- WeasyPrint for PDF invoices and receipts
- Resend for transactional email, Twilio for SMS and WhatsApp
- Sentry for error tracking
- Docker Compose for local development and production on a VPS, Cloudflare in front

Do not add React, Vue, Next.js, a separate SPA frontend, or Node.js build tooling. Ask before adding any new dependency.

## Project layout

```
config/            settings (base.py, dev.py, prod.py), urls.py, celery.py, wsgi.py, asgi.py
apps/
  core/            base templates, shared utilities, site pages (home, about, faq, contact)
  catalogue/       categories, items, images, packages
  inventory/       stock, condition, maintenance log
  bookings/        bookings, booking items, availability engine, quote builder
  payments/        Stripe integration, manual payments, caution deposits
  invoices/        invoice and receipt generation (PDF)
  notifications/   email, SMS and WhatsApp messages and reminders
  customers/       customer records
templates/         project-wide templates; app templates live in apps/<app>/templates/<app>/
static/            source static files (Tailwind input CSS, Alpine, HTMX)
tests/             cross-app tests; app tests live in apps/<app>/tests/
```

## Rules

### General
- Keep business logic in `services.py` modules inside each app, not in views, templates or model `save()` methods. Views stay thin.
- Use class-based or function-based views consistently within an app. Prefer function-based views with HTMX.
- All money is stored as `DecimalField(max_digits=10, decimal_places=2)` in USD. Never use floats for money.
- All datetimes are timezone-aware. Event dates are `DateField`.
- Every model has `created_at` and `updated_at` (use the abstract `TimeStampedModel` in `apps/core/models.py`).
- Write clear names and short docstrings. No commented-out code.

### Availability and bookings (critical)
- The same item unit must never be double-booked.
- Availability is calculated in `apps/bookings/services/availability.py` only.
- Any operation that reserves stock runs inside `transaction.atomic()` and locks the relevant rows with `select_for_update()`.
- Damaged and under-repair stock is never available.
- Cart holds expire automatically; expired holds release stock.

### Payments (critical)
- A booking is marked paid only from a verified Stripe webhook, never from the success redirect.
- Verify the Stripe webhook signature on every request. Webhook handlers must be idempotent.
- Never store card data. Use Stripe Checkout.
- Manual payments (Zelle, Cash App, bank deposit) are marked received by an admin, with amount, date and reference.

### Frontend
- Mobile-first. Design for a 375px wide screen first.
- Pages must stay light: no large JS bundles. Load analytics and pixel scripts after the page is interactive.
- Images: serve responsive sizes in WebP/AVIF, lazy-load below the fold, always set width and height.
- Use HTMX partial templates named `_partial_name.html`.
- Accessible markup: labels on every input, alt text on images, sufficient contrast.

### Performance budget (must work on slow 3G)
Every public page must stay within this budget on first visit. Check it before saying a page is done.

| Resource | Budget |
|---|---|
| Total page weight | under 500 KB |
| HTML document | under 50 KB (compressed) |
| CSS | under 30 KB (compressed) |
| JavaScript (excluding deferred tracking) | under 50 KB (compressed) |
| Hero / largest image | under 100 KB |
| Web fonts | system font stack, or at most one self-hosted WOFF2 font (2 weights max) |
| HTMX partial responses | under 15 KB |

Targets on Lighthouse mobile: Performance 90+, Largest Contentful Paint under 2.5 s on 4G, under 4 s on slow 3G, Cumulative Layout Shift under 0.1.

Rules:
- Use the system font stack by default. If a brand font is required, self-host it as WOFF2 with `font-display: swap` and preload only the main weight.
- No third-party CSS frameworks, icon fonts or UI libraries. Use inline SVG icons.
- Load HTMX and Alpine.js with `defer`. No other JS libraries without asking.
- Load Meta Pixel, Google Analytics and any chat widgets only after the page is interactive (on `load` or first user interaction).
- Images: generate responsive sizes (for example 400, 800, 1200 px wide) in AVIF and WebP with a JPEG fallback using `<picture>` and `srcset`. Set `width` and `height` on every image. Use `loading="lazy"` and `decoding="async"` for everything below the fold. Preload the hero image only.
- Show a tiny blurred placeholder (or solid colour) while images load so layout never jumps.
- Paginate catalogue lists (12 to 24 items per page). Never load all items at once.
- Use `select_related` and `prefetch_related` to avoid N+1 queries. A catalogue page must use no more than 10 database queries.
- Cache public pages and fragments (Django cache with Redis); set long cache headers on static files with hashed filenames (`ManifestStaticFilesStorage`).
- Keep HTMX responses as small partials, never full pages.
- After building or changing a public page, report its page weight, number of requests and query count, and flag anything over budget.

### Security
- Secrets come from environment variables only (`django-environ`). Never commit `.env`.
- `DEBUG=False` in production, secure cookies, HSTS, CSRF on all forms.
- Admin URL is not `/admin/`; it is set by the `ADMIN_URL` environment variable.

### Testing
- Use `pytest` and `pytest-django`, with `factory_boy` for test data.
- Every feature in bookings, availability and payments ships with tests, including edge cases (overlapping dates, simultaneous checkouts, duplicate webhooks, failed payments).
- Run the full test suite before saying a task is done.

### Code quality
- Format with `ruff format`, lint with `ruff check`. Fix all lint errors before finishing.
- Keep migrations small and named meaningfully.

## Commands

```
docker compose up -d                       # start Postgres, Redis, web, worker
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py tailwind watch
docker compose exec web pytest
docker compose exec web ruff check . && ruff format .
```

## How to work

1. Before writing code for a feature, give a short plan: models, services, views, templates and tests you will create or change.
2. Build one feature at a time. Stop when the feature works and tests pass.
3. Summarise what changed and anything that needs a decision from me.
4. Do not start the next feature until I say so.

## Build order (Phase 1)

1. Project setup (this file, Docker, settings, base layout)
2. Data model and admin
3. Website pages and catalogue
4. Availability engine
5. Quote builder and booking flow
6. Payments (Stripe and manual)
7. Invoices and receipts
8. Admin dashboard and inventory
9. Notifications and ad tracking
10. Launch hardening
