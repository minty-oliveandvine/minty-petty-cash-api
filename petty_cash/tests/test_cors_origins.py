"""``config/settings.py``'s CORS origins, read in a fresh interpreter (settings are read once at
import, and the test settings fix their own list). Two browser apps call this API: minty-petty-cash-web
and, since Petty Cash Settings moved into the hub (owner, 2026-11-10), minty-web."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_OURS = {"APP_ENV", "SECRET_KEY", "ACCOUNTS_API_URL", "PETTY_CASH_WEB_URL", "MINTY_WEB_URL",
         "CORS_ALLOWED_ORIGINS", "PREVIEW_ORIGIN_REGEX", "DJANGO_SETTINGS_MODULE"}


def _origins(env: dict) -> list[str]:
    clean = {k: v for k, v in os.environ.items() if k not in _OURS}
    proc = subprocess.run(
        [sys.executable, "-c", "import json, config.settings as s; print(json.dumps(s.CORS_ALLOWED_ORIGINS))"],
        # cwd outside the repo so load_dotenv() cannot pick up a developer's .env
        env={**clean, "PYTHONPATH": str(ROOT), "APP_ENV": "development", **env},
        cwd="/",
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_default_is_the_petty_cash_app_and_the_hub():
    assert _origins({}) == ["http://localhost:3010", "http://localhost:3000"]


def test_the_default_follows_both_addresses():
    assert _origins({"PETTY_CASH_WEB_URL": "https://pettycash.example/", "MINTY_WEB_URL": "https://my.example/"}) == [
        "https://pettycash.example",
        "https://my.example",
    ]


def test_an_explicit_list_replaces_the_default():
    assert _origins({"CORS_ALLOWED_ORIGINS": "https://a.example, https://b.example/"}) == [
        "https://a.example",
        "https://b.example",
    ]
