"""fc settings. Production values come from environment variables."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


DEBUG = env("FC_DEBUG", "0") == "1"
SECRET_KEY = env("FC_SECRET_KEY") or ("dev-only-key" if DEBUG else None)
if not SECRET_KEY:
    raise RuntimeError("Set FC_SECRET_KEY")

ALLOWED_HOSTS = env("FC_HOSTS", "localhost,127.0.0.1").split(",")
# The installation's name in the UI, which is its web domain.
FC_NAME = env("FC_NAME", ALLOWED_HOSTS[0])
# The address of the site in the links that fc prints, such as the link of a
# website. A local server with a port of its own sets it.
FC_URL = env("FC_URL", f"https://{FC_NAME}")
CSRF_TRUSTED_ORIGINS = [f"https://{h}" for h in ALLOWED_HOSTS]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "fc",
]

MIDDLEWARE = [
    # First, so that it sees each response, also an error of a middleware
    # below it, and a response under /link/ always gets a policy.
    "fc.middleware.SecurityHeaders",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "fcsite.urls"
WSGI_APPLICATION = "fcsite.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "builtins": ["fc.templatetags.fc"],
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": env("FC_DB", str(BASE_DIR / "fc.sqlite3")),
        # Several gunicorn threads write at once. A transaction takes the write
        # lock when it begins (IMMEDIATE), so two that read and then write take
        # turns instead of failing with "database is locked", and WAL lets
        # readers read while one thread writes.
        "OPTIONS": {
            "timeout": 20,
            "transaction_mode": "IMMEDIATE",
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
        },
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
# Login is by a code sent to the email (fc/enter.py).
LOGIN_URL = "/"

# Email goes through SMTP in production, and to the console in development.
if env("FC_SMTP_PASSWORD"):
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = env("FC_SMTP_HOST", "smtp.resend.com")
    # Port 587 with STARTTLS. Some hosts block outbound 465.
    EMAIL_PORT = int(env("FC_SMTP_PORT", "587"))
    EMAIL_USE_SSL = EMAIL_PORT == 465
    EMAIL_USE_TLS = not EMAIL_USE_SSL
    EMAIL_TIMEOUT = 15
    EMAIL_HOST_USER = env("FC_SMTP_USER", "resend")
    EMAIL_HOST_PASSWORD = env("FC_SMTP_PASSWORD")
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = env("FC_FROM", f"{FC_NAME} <fc@{FC_NAME}>")

FC_BRIDGE_SOCKET = env("FC_BRIDGE_SOCKET", "/run/fc/bridge.sock")
# The network's main CES server (host:port), which the sky draws in a style of
# its own. Empty: none.
FC_MAIN_SERVER = env("FC_MAIN_SERVER", "")
# Text at the end of the chat model's rules, for this installation. Empty: none.
FC_CHAT_CONTEXT = env("FC_CHAT_CONTEXT", "")
# This installation's ledger, by its public address (host:port).
FC_LEDGER = env("FC_LEDGER", "127.0.0.1:55100" if DEBUG else None)
if not FC_LEDGER:
    raise RuntimeError("Set FC_LEDGER")
# API calls per minute for each account.
FC_API_RATE = int(env("FC_API_RATE", "240"))
# The site sends every change to the hub (fc/hub.py) on this socket, and
# pages connect at FC_HUB_URL (a path on this site, or a ws:// URL in
# development).
FC_HUB_SOCKET = env("FC_HUB_SOCKET", "/run/fc-hub/hub.sock")
FC_HUB_URL = env("FC_HUB_URL", "/hub")
# The bridge's cesh socket, which runs cesh as the person through ceshd.
FC_CESH_SOCKET = env("FC_CESH_SOCKET", "/run/fc/cesh.sock")
# The base URL of this installation's miner builds, for the miner tab, as
# https://github.com/<owner>/<repo>/releases/latest/download. Empty: no tab.
FC_MINER_DOWNLOADS = env("FC_MINER_DOWNLOADS", "").rstrip("/")
# The longest wait for one model answer (the think server ends a call at its
# think_timeout_secs, 300 by default).
FC_THINK_TIMEOUT = int(env("FC_THINK_TIMEOUT", "320"))
# The stats report (fc/stats.py), which the unit fc-stats writes in memory.
FC_STATS_FILE = env("FC_STATS_FILE", str(BASE_DIR.parent / "tmp" / "stats.json") if DEBUG else "/run/fc-stats/report.json")

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = 3600
# A login lasts a year from the last visit.
SESSION_COOKIE_AGE = 365 * 24 * 3600
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_HTTPONLY = True

# Rate limits live in the cache, so all app workers must share one cache.
if env("FC_CACHE_DIR"):
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
                          "LOCATION": env("FC_CACHE_DIR")}}

STATIC_URL = "/static/"
if not DEBUG:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
    }
STATIC_ROOT = env("FC_STATIC_ROOT", str(BASE_DIR / "static-root"))

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}

# For local testing: an email enters at once, with no code. Only a debug
# server reads this setting.
FC_LOGIN_WITHOUT_CODE = DEBUG and env("FC_LOGIN_WITHOUT_CODE", "0") == "1"
