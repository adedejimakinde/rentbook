"""Settings shared by every environment. All configuration comes from env vars."""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
env_file = BASE_DIR / ".env"
if env_file.exists():
    env.read_env(str(env_file))

SECRET_KEY = env("SECRET_KEY")
DEBUG = env.bool("DEBUG", default=False)
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

BUSINESS_NAME = env("BUSINESS_NAME", default="Atglace Events & Rentals LLC")
BUSINESS_TAGLINE = env("BUSINESS_TAGLINE", default="Your one-stop shop for event rentals")
BUSINESS_AREA = env("BUSINESS_AREA", default="DFW area")
BUSINESS_PHONE = env("BUSINESS_PHONE", default="469-347-4687")
BUSINESS_EMAIL = env("BUSINESS_EMAIL", default="atglaceevents.rental@gmail.com")
WHATSAPP_NUMBER = env("WHATSAPP_NUMBER", default="14693474687")

UNFOLD = {
    "SITE_TITLE": BUSINESS_NAME,
    "SITE_HEADER": BUSINESS_NAME,
}
