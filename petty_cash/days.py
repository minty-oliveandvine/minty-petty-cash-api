"""The reporting day - on ``settings.REPORT_TZ``'s calendar (every backend's default zone,
``minty_shared.time``), whatever the server's own zone."""

from __future__ import annotations

from datetime import date, datetime

from django.conf import settings
from minty_shared import time as shared_time


def today() -> date:
    return shared_time.today_in(settings.REPORT_TZ)


def parse_day(raw) -> date | None:
    """A ``YYYY-MM-DD`` string as a date, or None for anything else (Flask's ``_day``)."""
    return shared_time.parse_day(raw)


def iso(value: date | None) -> str | None:
    return shared_time.iso(value)


def hk(moment: datetime | None) -> datetime | None:
    """A stored moment on the reporting calendar (a naive one is UTC), as Flask's
    ``report_page._hk``."""
    if moment is None:
        return None
    return shared_time.in_zone(moment, settings.REPORT_TZ)
