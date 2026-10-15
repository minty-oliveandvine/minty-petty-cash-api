"""The reporting day - Hong Kong's (Flask's ``models.db.tz``), whatever the server's own zone."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from django.conf import settings

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def today() -> date:
    return datetime.now(ZoneInfo(settings.REPORT_TZ)).date()


def parse_day(raw) -> date | None:
    """A ``YYYY-MM-DD`` string as a date, or None for anything else (Flask's ``_day``)."""
    if not isinstance(raw, str) or not _ISO_DATE.match(raw):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def hk(moment: datetime | None) -> datetime | None:
    """A stored moment in Hong Kong time (a naive one is UTC), as Flask's ``report_page._hk``."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(ZoneInfo(settings.REPORT_TZ))
