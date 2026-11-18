"""Settings shared by every environment. All configuration comes from env vars."""

from decimal import Decimal
from pathlib import Path

import environ

from config.sentry import init_sentry

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
env_file = BASE_DIR / ".env"
if env_file.exists():
    env.read_env(str(env_file))

SECRET_KEY = env("SECRET_KEY")
DEBUG = env.bool("DEBUG", default=False)
# True only in config.settings.prod; production-only system checks look at it.
IS_PRODUCTION = False
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])

ADMIN_URL = env("ADMIN_URL").strip("/") + "/"
if ADMIN_URL == "admin/":
    raise ValueError("ADMIN_URL must not be 'admin/'. Choose a private path.")

TIME_ZONE = env("TIME_ZONE", default="America/Chicago")
USE_TZ = True
LANGUAGE_CODE = "en-us"
USE_I18N = False

INSTALLED_APPS = [
    "unfold",
    "unfold.contrib.filters",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sitemaps",
    "django_tailwind_cli",
    "apps.core",
    "apps.catalogue",
    "apps.inventory",
    "apps.bookings",
    "apps.payments",
    "apps.invoices",
    "apps.notifications",
    "apps.customers",
    "apps.dashboard",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.business",
                "apps.core.context_processors.site_sections",
                "apps.bookings.context_processors.cart",
                "apps.dashboard.context_processors.staff_nav",
            ],
        },
    },
]

DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgres://rentbook:rentbook@localhost:5432/rentbook",
    ),
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    },
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

TAILWIND_CLI_SRC_CSS = "assets/css/source.css"
TAILWIND_CLI_DIST_CSS = "css/tailwind.css"

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default=REDIS_URL)
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default=REDIS_URL)
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_BEAT_SCHEDULE = {
    "release-expired-holds": {
        "task": "bookings.release_expired_holds",
        "schedule": 60.0,
    },
    "retry-unmatched-stripe-events": {
        "task": "payments.retry_unmatched_stripe_events",
        "schedule": 15 * 60,
    },
    "sync-stale-payments": {
        "task": "payments.sync_stale_payments",
        "schedule": 15 * 60,
    },
    "issue-missing-documents": {
        "task": "invoices.issue_missing_documents",
        "schedule": 15 * 60,
    },
    "sync-recent-paid-bookings": {
        "task": "payments.sync_recent_paid_bookings",
        "schedule": 60 * 60,
    },
    "run-reminders": {
        "task": "notifications.run_reminders",
        "schedule": 15 * 60,
    },
    "send-due-notifications": {
        "task": "notifications.send_due_notifications",
        "schedule": 5 * 60,
    },
}

# Days before and after an event during which its items stay unavailable.
BOOKING_BUFFER_DAYS = env.int("BOOKING_BUFFER_DAYS", default=0)
# Minutes a held booking keeps its stock before the hold lapses.
HOLD_MINUTES = env.int("HOLD_MINUTES", default=15)
# Hours a hold placed by staff in the admin lasts when no expiry is entered.
ADMIN_HOLD_HOURS = env.int("ADMIN_HOLD_HOURS", default=48)
# Hours between now and the start of the event day that a quote or online
# reservation needs. Staff bookings in the admin are not held to this.
MIN_NOTICE_HOURS = env.int("MIN_NOTICE_HOURS", default=24)

# Payments. Stripe keys come from the environment only.
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY", default="")
STRIPE_PUBLISHABLE_KEY = env("STRIPE_PUBLISHABLE_KEY", default="")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", default="")
# Percentage of the booking total taken as the deposit.
DEPOSIT_PERCENT = env.int("DEPOSIT_PERCENT", default=50)
if not 1 <= DEPOSIT_PERCENT <= 100:
    raise ValueError("DEPOSIT_PERCENT must be between 1 and 100.")
# Days a hold is kept while an ACH bank payment is still clearing.
# Sales tax as a percent (8.25 means 8.25%). Applies to items and packages after
# any discount; delivery and setup fees are taxed only when switched on below.
SALES_TAX_RATE = Decimal(env.str("SALES_TAX_RATE", default="0"))
if not 0 <= SALES_TAX_RATE <= 100:
    raise ValueError("SALES_TAX_RATE must be between 0 and 100.")
TAX_DELIVERY = env.bool("TAX_DELIVERY", default=False)
TAX_SETUP = env.bool("TAX_SETUP", default=False)

ACH_HOLD_DAYS = env.int("ACH_HOLD_DAYS", default=7)
# Hours a booking paid by Zelle, Cash App or bank deposit stays held while staff
# wait for the money. Staff can extend a hold in the admin.
MANUAL_PAY_HOLD_HOURS = env.int("MANUAL_PAY_HOLD_HOURS", default=24)
# Payment details shown to customers who choose a manual method.
ZELLE_RECIPIENT = env("ZELLE_RECIPIENT", default="")
ZELLE_NAME = env("ZELLE_NAME", default="")
CASHAPP_TAG = env("CASHAPP_TAG", default="")
BANK_NAME = env("BANK_NAME", default="")
BANK_ACCOUNT_NAME = env("BANK_ACCOUNT_NAME", default="")
BANK_ACCOUNT_NUMBER = env("BANK_ACCOUNT_NUMBER", default="")
BANK_ROUTING_NUMBER = env("BANK_ROUTING_NUMBER", default="")

# Signed links sent to customers. A balance link lasts until the end of the
# event date or BALANCE_LINK_DAYS from when it was made, whichever is later.
BALANCE_LINK_DAYS = env.int("BALANCE_LINK_DAYS", default=14)
STATUS_LINK_DAYS = env.int("STATUS_LINK_DAYS", default=180)

# Version of the rental terms customers accept at checkout. Change it whenever
# the terms page changes; each booking records the version accepted.
TERMS_VERSION = env("TERMS_VERSION", default="2026-11-draft")

BUSINESS_NAME = env("BUSINESS_NAME", default="Atglace Events & Rentals LLC")
BUSINESS_TAGLINE = env("BUSINESS_TAGLINE", default="Your one-stop shop for event rentals")
BUSINESS_AREA = env("BUSINESS_AREA", default="DFW area")
BUSINESS_PHONE = env("BUSINESS_PHONE", default="469-347-4687")
BUSINESS_EMAIL = env("BUSINESS_EMAIL", default="atglaceevents.rental@gmail.com")
WHATSAPP_NUMBER = env("WHATSAPP_NUMBER", default="14693474687")
# Social profile links for the footer; a link is shown only when its URL is set.
SOCIAL_INSTAGRAM_URL = env("SOCIAL_INSTAGRAM_URL", default="")
SOCIAL_TIKTOK_URL = env("SOCIAL_TIKTOK_URL", default="")
SOCIAL_FACEBOOK_URL = env("SOCIAL_FACEBOOK_URL", default="")
# Public URL of the site, used for absolute links in the sitemap, Open Graph and JSON-LD.
SITE_URL = env("SITE_URL", default="http://localhost:8000").rstrip("/")

EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="localhost")
EMAIL_PORT = env.int("EMAIL_PORT", default=25)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=False)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default=BUSINESS_EMAIL)
# Resend, through django-anymail, sends email in production (see prod.py).
RESEND_API_KEY = env("RESEND_API_KEY", default="")
ANYMAIL = {"RESEND_API_KEY": RESEND_API_KEY}


# Error tracking. Sentry starts only when SENTRY_DSN is set; leave it empty in development.
SENTRY_DSN = env("SENTRY_DSN", default="")
init_sentry(
    SENTRY_DSN,
    environment=env("SENTRY_ENVIRONMENT", default="production"),
    traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0),
)


def _static(path):
    from django.templatetags.static import static

    return static(path)


UNFOLD = {
    "SITE_TITLE": BUSINESS_NAME,
    "SITE_HEADER": BUSINESS_NAME,
    "SITE_URL": "/",
    # Brand purple from docs/design/tokens.json (brand is 600, brand-deep 800).
    "COLORS": {
        "primary": {
            "50": "#f7f4fa",
            "100": "#f0e5fb",
            "200": "#e0c8f5",
            "300": "#c79cec",
            "400": "#a863de",
            "500": "#8a2bd0",
            "600": "#6a0dad",
            "700": "#5a0b94",
            "800": "#4e0a84",
            "900": "#3c0766",
            "950": "#2a0549",
        },
    },
    "STYLES": [lambda request: _static("admin/staff.css")],
    # Every admin page: forms submit once, so a double click cannot save twice.
    "SCRIPTS": [lambda request: _static("core/js/submit_once.js")],
}

# Text messages through Twilio. SMS goes only to customers who ticked the consent
# box; WhatsApp stays off unless WHATSAPP_ENABLED is true and the message kind has
# an approved template. Owner alerts go to OWNER_PHONE.
TWILIO_ACCOUNT_SID = env("TWILIO_ACCOUNT_SID", default="")
TWILIO_AUTH_TOKEN = env("TWILIO_AUTH_TOKEN", default="")
TWILIO_FROM_NUMBER = env("TWILIO_FROM_NUMBER", default="")
WHATSAPP_ENABLED = env.bool("WHATSAPP_ENABLED", default=False)
TWILIO_WHATSAPP_FROM = env("TWILIO_WHATSAPP_FROM", default="")
# Approved WhatsApp templates (Twilio Content SIDs) by message kind, as
# kind=HX...,kind=HX... A kind with no template is not sent on WhatsApp.
WHATSAPP_TEMPLATES = env.dict("WHATSAPP_TEMPLATES", default={})
OWNER_PHONE = env("OWNER_PHONE", default="")
# Days before the event the first balance reminder goes out.
BALANCE_DUE_DAYS_BEFORE = env.int("BALANCE_DUE_DAYS_BEFORE", default=7)
# Link in the thank-you message after the event; no thank-you is sent without it.
REVIEW_URL = env("REVIEW_URL", default="")
