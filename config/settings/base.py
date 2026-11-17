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
    "sync-recent-paid-bookings": {
        "task": "payments.sync_recent_paid_bookings",
        "schedule": 60 * 60,
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
    # Every admin page: forms submit once, so a double click cannot save twice.
    "SCRIPTS": [lambda request: _static("core/js/submit_once.js")],
}
