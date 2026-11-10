"""config/settings refuses to boot without accounts-api's address outside development.

The Xero token service lives there; a silent default would fail every new supplier quietly.
"""

import importlib
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured


def _load_settings(monkeypatch, env):
    for k in ("SECRET_KEY", "APP_ENV", "ACCOUNTS_API_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # load_dotenv() must not put a real value back from .env for this test.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: False)
    sys.modules.pop("config.settings", None)
    return importlib.import_module("config.settings")


@pytest.fixture(autouse=True)
def _restore_settings_module():
    yield
    # Put the test settings module back for the rest of the suite.
    sys.modules.pop("config.settings", None)
    importlib.import_module("config.settings_test")


def test_accounts_api_url_is_required_outside_development(monkeypatch):
    with pytest.raises(ImproperlyConfigured, match="ACCOUNTS_API_URL is required"):
        _load_settings(monkeypatch, {"APP_ENV": "production", "SECRET_KEY": "x" * 64})


def test_accounts_api_url_boots_in_production_when_set(monkeypatch):
    mod = _load_settings(
        monkeypatch,
        {"APP_ENV": "production", "SECRET_KEY": "x" * 64, "ACCOUNTS_API_URL": "https://accounts-api.example/"},
    )
    assert mod.ACCOUNTS_API_URL == "https://accounts-api.example"


def test_accounts_api_url_defaults_to_local_accounts_api_in_development(monkeypatch):
    mod = _load_settings(monkeypatch, {"APP_ENV": "development"})
    assert mod.ACCOUNTS_API_URL == "http://localhost:8000"
