"""Nothing under this service imports the Flask world.

The routes and services are ported from Flask's ``blueprints/petty_cash_api`` and ``blueprints/report/services``, and a
verbatim copy carries its imports with it: ``flask.g``, ``flask_mail``, ``loguru``, the
SQLAlchemy ``db`` handle, ``blueprints.*``. None of those packages is installed here, so a
leftover would fail at import - but only when that module is first imported, which for a
function-local import can be months after the port. This walks the source instead.
"""

from __future__ import annotations

from pathlib import Path

from minty_shared.testing import guards

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ("petty_cash", "core", "shared_models", "config")


def test_no_flask_world_import():
    assert guards.forbidden_imports(ROOT, PACKAGES) == []


def test_the_walk_saw_the_service():
    files = {p.relative_to(ROOT).as_posix() for p in guards.python_files(ROOT, PACKAGES)}
    assert "core/auth.py" in files and "shared_models/models.py" in files
