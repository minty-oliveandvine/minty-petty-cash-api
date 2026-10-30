import os

# The placeholder SECRET_KEY refuses to boot outside development; the real test key is set
# below, after the import that would otherwise trip the guard.
os.environ.setdefault("APP_ENV", "development")

from config.dburl import parse_database_url  # noqa: E402
from config.settings import *  # noqa: E402, F401, F403

# A fixed key so tests can mint the module token Flask would. Nothing about the value matters
# except that signing and verifying use the same one - the whole contract with Flask.
SECRET_KEY = "test-secret-key-shared-with-flask"

# Two test databases, chosen by MINTY_TEST_PG_URI (the same knobs as the other Django APIs):
#
#   unset  -> SQLite in memory, tables built FROM THE MODELS (MIGRATION_MODULES has minty_db: None).
#   set    -> PostgreSQL, a database built from minty-db's packaged 01_schema_rebased.sql by
#             minty_db.testing.pg (the root conftest.py's plugin). A model column the schema
#             lacks fails on the SELECT, which is the point. MINTY_TEST_PG_DBNAME
#             (minty_test - always give a unique one), MINTY_TEST_PG_KEEP=1.
_PG_URI = os.environ.get("MINTY_TEST_PG_URI")
if _PG_URI:
    from urllib.parse import parse_qs as _parse_qs
    from urllib.parse import urlsplit as _urlsplit

    _db, _schema = parse_database_url(_PG_URI)
    if "schema" in _parse_qs(_urlsplit(_PG_URI).query):
        DB_SCHEMA = _schema
    _db["OPTIONS"]["options"] = f"-c search_path={DB_SCHEMA},public"  # noqa: F405
    _db["NAME"] = os.environ.get("MINTY_TEST_PG_DBNAME", "minty_test")
    _db["TEST"] = {"NAME": _db["NAME"]}
    DATABASES = {"default": _db}
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

LOGGING["handlers"]["file_core"] = {"class": "logging.NullHandler"}  # noqa: F405
LOGGING["handlers"]["file_api"] = {"class": "logging.NullHandler"}  # noqa: F405


# Fixed values, never the developer's .env: a CORS test must not pass or fail by whose
# machine ran it, and no test may reach a real Flask or bucket.
ACCOUNTS_API_URL = "http://flask.invalid"
PETTY_CASH_WEB_URL = "http://localhost:3010"
CORS_ALLOWED_ORIGINS = [PETTY_CASH_WEB_URL]
S3 = {"endpoint_url": "http://s3.invalid", "bucket": "test-receipts", "key": "k", "secret": "s",
      "region": "us-east-1"}
