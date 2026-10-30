import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv
from minty_shared.logctx import logging_config

from config.dburl import database_url, parse_database_url
from config.preview_origin import preview_origin_regexes
from config.s3url import parse_s3_url

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Shared with the Flask app (Minty) and the other three Django APIs. Flask MINTS the JWTs this
# service verifies (``core/auth.py``) through its login-gated ``/handoff/petty-cash-web``; this
# service never mints one, and a mismatch 401s every request.
_DEFAULT_SECRET_KEY = "change-me-in-production"
SECRET_KEY = os.environ.get("SECRET_KEY", _DEFAULT_SECRET_KEY)
# APP_ENV is ``development`` or ``production`` (the default); anything else is production.
APP_ENV = os.environ.get("APP_ENV", "production").strip().lower()
DEBUG = APP_ENV == "development"

# Refuse to boot with the placeholder key outside development (the 2026-09-14 lesson: a
# missing key answered 401 everywhere and said nothing - and the fallback is in the repo).
if not DEBUG and SECRET_KEY == _DEFAULT_SECRET_KEY:
    raise ImproperlyConfigured(
        "SECRET_KEY is not set. It must be the same value the Flask app mints tokens "
        "with; without it every request is refused with 401. Refusing to start."
    )
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "*").split(",")

# No django.contrib.auth / contenttypes: authentication is the bearer token (core.auth),
# authorisation Flask's policy (core.policy). Installing them would make `migrate` want tables
# in a schema Minty owns - and this service never runs migrate (docker/entrypoint.sh).
INSTALLED_APPS = [
    "corsheaders",
    "minty_db",
    "petty_cash.apps.PettyCashConfig",
]
# minty_db's models are the schema (minty-db, generated from 01_schema_rebased.sql). Its
# migrations are run by hand (python -m minty_db migrate), never by a service: None here keeps
# `migrate` from ever touching them, and lets a SQLite test run build the tables from the models.
MIGRATION_MODULES = {"minty_db": None}

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "core.middleware.NoStoreMiddleware",
    "core.middleware.RequestLoggingMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = []

# ---------------------------------------------------------------------------
# Cross-service: the Flask app (Minty) - the token minter, the hand-off and Xero's only
# credential holder, becoming accounts-api. Asked for a Xero access token (core/xero_tokens.py);
# locally on 8000 (the owner's ports of 2026-10-14). ``PETTY_CASH_URL`` is NOT read: that name is
# being freed for this service. Outside development there is no default - a wrong one would fail
# every Xero call quietly.
# ---------------------------------------------------------------------------
ACCOUNTS_API_URL = (os.environ.get("ACCOUNTS_API_URL") or ("http://localhost:8000" if DEBUG else "")).rstrip("/")
if not ACCOUNTS_API_URL:
    raise ImproperlyConfigured(
        "ACCOUNTS_API_URL is not set. It is the Flask app's address, which hands out Xero access "
        "tokens; without it adding a supplier fails. Refusing to start."
    )
#: Xero's accounting API (Flask's ``XERO_API_BASE_URL``).
XERO_API_BASE_URL = (os.environ.get("XERO_API_BASE_URL") or "https://api.xero.com/api.xro/2.0").rstrip("/")
XERO_TOKEN_TIMEOUT = 15

# ---------------------------------------------------------------------------
# CORS - one browser app calls this API: minty-petty-cash-web (``PETTY_CASH_WEB_URL``), plus a
# branch's preview set (``PREVIEW_ORIGIN_REGEX``). Flask's ``petty_cash_api/api.py`` stamps the
# same origins; ``x-entity-id`` is NOT advertised - the company is the URL's, never a header's.
# ---------------------------------------------------------------------------
PETTY_CASH_WEB_URL = (os.environ.get("PETTY_CASH_WEB_URL") or "http://localhost:3010").rstrip("/")
CORS_ALLOWED_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in (os.environ.get("CORS_ALLOWED_ORIGINS") or PETTY_CASH_WEB_URL).split(",")
    if origin.strip()
]
CORS_ALLOWED_ORIGIN_REGEXES = preview_origin_regexes(os.environ.get("PREVIEW_ORIGIN_REGEX"))
CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
CORS_ALLOW_HEADERS = ["authorization", "content-type", "accept", "origin"]
CORS_EXPOSE_HEADERS = ["Content-Disposition"]

# ---------------------------------------------------------------------------
# Database - the schema Minty owns. Every model is managed = False, no migrations; the schema
# name is ``?schema=`` on DATABASE_URL (default pettycashv3, config/dburl.py), and every
# db_table is unqualified and resolves through search_path.
# ---------------------------------------------------------------------------
_DEFAULT_DB, DB_SCHEMA = parse_database_url(database_url())
DATABASES = {"default": _DEFAULT_DB}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LANGUAGE_CODE = "en-us"
USE_I18N = False
USE_TZ = True
TIME_ZONE = "UTC"
#: The reporting day is Hong Kong's (Flask's ``models.db.tz``): "today" for the date rules.
REPORT_TZ = "Asia/Hong_Kong"

# ---------------------------------------------------------------------------
# Receipts - Backblaze B2 through the S3 API, one URL (``S3_URL``, Flask's).
# ---------------------------------------------------------------------------
S3 = parse_s3_url(os.environ.get("S3_URL"))
#: The largest receipt an expense line takes (Flask's "PDF, JPEG, PNG (Max 10MB)").
MAX_RECEIPT_BYTES = 10 * 1024 * 1024
# Django refuses a multipart body over its own limit before a view sees it; the receipt rule
# (10 MB, answered with Flask's sentence) is checked in the view, so allow a little over it.
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_RECEIPT_BYTES + 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = MAX_RECEIPT_BYTES + 1024 * 1024

# ---------------------------------------------------------------------------
# Logging - one line format for every Minty backend (minty_shared.logctx):
#   <UTC>Z LEVEL [trace=…] [user=…|entity=…|step=…] message
# ---------------------------------------------------------------------------
LOG_DIR = BASE_DIR / "logs"

LOGGING = logging_config(
    {"petty-cash-api": "core.log", "petty-cash-api.http": "api.log"},
    log_dir=LOG_DIR,
    no_propagate=["petty-cash-api.http"],
    level=os.environ.get("LOG_LEVEL", "INFO"),
)
