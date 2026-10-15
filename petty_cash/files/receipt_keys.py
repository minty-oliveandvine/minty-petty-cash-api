"""Receipt key helpers - Flask's ``blueprints/report/services/receipt_keys.py``, copied: new uploads
are named ``[A-Z0-9_]`` only (``safe_stem``), and ``split_receipt_keys`` reads old comma-joined values."""

from __future__ import annotations

import re

_KEY_BOUNDARY = re.compile(r",(?=\s*(?:expenses/|attachments/|uploads/|https?://))")


def split_receipt_keys(value: str | None) -> list[str]:
    """The keys in a comma-separated ``files`` value, one per receipt, commas in names kept."""
    if not value:
        return []
    return [k.strip() for k in _KEY_BOUNDARY.split(value) if k.strip()]


_SAFE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "pdf"}
_STEM_MAX = 80


def safe_stem(text: str | None) -> str:
    """A filename stem from free text: uppercase, ``&`` -> ``AND``, every run of anything
    outside ``[A-Z0-9]`` -> one ``_``, no leading/trailing ``_``, at most 80 characters.

    ``"Staff Welfare - Meal, Transport etc"`` -> ``"STAFF_WELFARE_MEAL_TRANSPORT_ETC"``.
    Empty input gives ``""`` so the caller can fall back to something else.
    """
    t = (text or "").upper().replace("&", " AND ")
    t = re.sub(r"[^A-Z0-9]+", "_", t).strip("_")
    return t[:_STEM_MAX].rstrip("_")


def safe_extension(filename: str | None, default: str = "jpg") -> str:
    """The extension to store under, without the dot, from the known set; else the default."""
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    return ext if ext in _SAFE_EXTENSIONS else default
