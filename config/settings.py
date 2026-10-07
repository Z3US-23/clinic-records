"""
Django settings for Clinic Records.

Configuration comes from environment variables. For local development, copy
`.env.example` to `.env` — it is read automatically below. In production set
the variables on the hosting platform instead (never commit a real `.env`).
"""

import os
from pathlib import Path
from urllib.parse import urlsplit

import dj_database_url
from django.contrib.messages import constants as message_constants
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path):
    """Minimal .env reader: KEY=VALUE lines; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


# --- Core -------------------------------------------------------------------

# Secure by default: DEBUG is off unless explicitly switched on (see .env.example).
DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "dev-only-insecure-key-change-me"
    else:
        raise ImproperlyConfigured("Set DJANGO_SECRET_KEY (required when DJANGO_DEBUG is off).")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")
if DEBUG:
    ALLOWED_HOSTS += ["localhost", "127.0.0.1", "[::1]", "testserver"]

CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "whitenoise.runserver_nostatic",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # Project apps
    "apps.core",
    "apps.accounts",
    "apps.patients",
    "apps.clinical",
    "apps.appointments",
    "apps.reminders",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.CurrentClinicMiddleware",
    # After authentication and messages: sends people whose password was set by a clinic
    # owner to "Change password" before anything else.
    "apps.accounts.middleware.PasswordChangeRequiredMiddleware",
    "apps.core.middleware.PrivateCacheControlMiddleware",
]

ROOT_URLCONF = "config.urls"

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
                "apps.core.context_processors.app_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# SQLite locally; set DATABASE_URL (PostgreSQL) in production.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Auth -------------------------------------------------------------------

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

# Failed sign-in lockout (see apps/accounts/lockout.py). Three limits, each counted over
# LOGIN_LOCKOUT_MINUTES; reaching any of them refuses those sign-ins for that long:
#   LOGIN_MAX_ATTEMPTS            one email from one IP address (a typo-prone person, or a guesser)
#   LOGIN_MAX_ATTEMPTS_PER_EMAIL  one email from any IP address (guessing from many addresses)
#   LOGIN_MAX_ATTEMPTS_PER_IP     one IP address with any email ("password spraying")
LOGIN_MAX_ATTEMPTS = int(env("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_MAX_ATTEMPTS_PER_EMAIL = int(env("LOGIN_MAX_ATTEMPTS_PER_EMAIL", "20"))
LOGIN_MAX_ATTEMPTS_PER_IP = int(env("LOGIN_MAX_ATTEMPTS_PER_IP", "30"))
LOGIN_LOCKOUT_MINUTES = int(env("LOGIN_LOCKOUT_MINUTES", "15"))

# A working day; the timer restarts on every request.
SESSION_COOKIE_AGE = 60 * 60 * 8
SESSION_SAVE_EVERY_REQUEST = True

# --- Internationalisation ---------------------------------------------------

LANGUAGE_CODE = "en-gb"  # day-month-year dates, as used in Pakistan and India
TIME_ZONE = "Asia/Karachi"  # default; each clinic's own timezone is activated per request
USE_I18N = True
USE_TZ = True

# --- Static & uploaded files ------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

# Uploaded lab reports are PRIVATE: there is deliberately no URL route for
# MEDIA_ROOT. Files are only served through a login-checked, clinic-scoped view.
MEDIA_ROOT = Path(env("MEDIA_ROOT", str(BASE_DIR / "private_media")))
MEDIA_URL = "/private-media-not-served/"
LAB_UPLOAD_MAX_MB = 10
LAB_UPLOAD_EXTENSIONS = ["pdf", "jpg", "jpeg", "png"]

# --- Security (production) --------------------------------------------------

SECURE_REFERRER_POLICY = "same-origin"  # keeps confirmation-link tokens out of Referer headers
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(env("DJANGO_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True

SESSION_COOKIE_HTTPONLY = True

# Behind a hosting platform's proxy (Render, Railway, Heroku, nginx) every request
# arrives from the proxy's address. Turn this on there so the audit log and the
# failed-sign-in lockout see each person's real IP address. Leave it OFF when the
# app is reached directly: then the header could be faked by anyone.
USE_X_FORWARDED_FOR = env_bool("DJANGO_USE_X_FORWARDED_FOR", False)
# How many trusted proxies add themselves to X-Forwarded-For. The client's address
# is read that many places from the RIGHT, because the left end can be faked.
TRUSTED_PROXY_COUNT = int(env("DJANGO_TRUSTED_PROXY_COUNT", "1"))

# --- Misc -------------------------------------------------------------------


def _cache_config(url):
    """DJANGO_CACHE_URL: empty = in-memory (one computer), "db" = database table, redis://... = Redis.

    The failed-sign-in lockout counts attempts in this cache, so in production every
    server process must share it: use "db" (run `manage.py createcachetable` once) or Redis.
    """
    if not url:
        return {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    if url in {"db", "database"}:
        return {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "django_cache"}
    if url.startswith(("redis://", "rediss://")):
        return {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": url}
    raise ImproperlyConfigured('DJANGO_CACHE_URL must be empty, "db", or a redis:// URL.')


CACHES = {"default": _cache_config(env("DJANGO_CACHE_URL", "").strip())}

EMAIL_BACKEND = env("DJANGO_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")

MESSAGE_TAGS = {message_constants.ERROR: "danger"}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", "INFO")},
}

# --- Product settings -------------------------------------------------------

PRODUCT_NAME = env("PRODUCT_NAME", "Clinic Records")

# Public "Register your clinic" page. Off by default: turn it on for demo sites only.
# A pilot with real patients onboards each clinic by hand.
ALLOW_CLINIC_SIGNUP = env_bool("ALLOW_CLINIC_SIGNUP", False)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _site_url(value, debug):
    """SITE_URL: the public address patients' confirmation links point to.

    Every appointment reminder stores the link inside its WhatsApp text, so a wrong address would
    send patients to a dead page. Outside development it must be the real https:// address, with
    no path, e.g. https://yourapp.onrender.com.
    """
    url = (value or "").strip().rstrip("/")
    if not url:
        if debug:
            return "http://127.0.0.1:8000"
        raise ImproperlyConfigured(
            "Set SITE_URL to the app's public address, e.g. https://yourapp.onrender.com "
            "(required when DJANGO_DEBUG is off)."
        )
    parts = urlsplit(url)
    if not debug and (
        parts.scheme != "https"
        or not parts.hostname
        or parts.hostname in _LOCAL_HOSTS
        or parts.path
        or parts.query
        or parts.fragment
    ):
        raise ImproperlyConfigured(
            f"SITE_URL must be the public https:// address with no path, e.g. https://yourapp.onrender.com "
            f"(it is {url!r})."
        )
    return url


# Used to build absolute links (e.g. appointment confirmation links in WhatsApp
# messages) when there is no incoming request, such as in scheduled jobs.
SITE_URL = _site_url(env("SITE_URL"), DEBUG)
