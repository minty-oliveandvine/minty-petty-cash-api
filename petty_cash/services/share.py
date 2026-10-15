"""A read-only link to a submitted day - Flask's ``blueprints/report/services/share.py``, ported.
Flask SERVES the link (``/Minty_Report/{initials}/{date}/{secret}/``, ``report/routes/legacy.py``),
so the row and the token are made exactly as Flask makes them, and the address is Flask's
(``ACCOUNTS_API_URL``). One row per company-day: asking again renews its token and expiry and keeps
its address. The secret, not the readable prefix, is what grants access.

Changed: ``expires_at`` is an aware UTC time (Flask stored its server's naive local time into a
time-zone column).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
from datetime import UTC, date, datetime, timedelta

from django.conf import settings
from django.db import transaction

from shared_models.models import Entity, ShareLink, UserEntity

logger = logging.getLogger("petty-cash-api")

SHARE_LINK_HOURS = 720
_SECRET_BYTES = 24  # -> 32 url-safe characters
SHARE_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")


class ShareRefused(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def is_share_path(path_segment) -> bool:
    """True for the "{initials}/{date}/{secret}" form; the old secret-less form is refused."""
    parts = (path_segment or "").strip("/").split("/")
    return len(parts) == 3 and all(parts) and bool(SHARE_SECRET_RE.match(parts[2]))


def acronym(name: str | None) -> str:
    """Flask's ``build_entity_acronym(letters_only=True)``: the first letter of each word that
    starts with a Latin letter, upper-cased."""
    return "".join(word[0].upper() for word in (name or "").split() if word and re.match(r"[A-Za-z]", word[0]))


def share_token(entity_id, day: date, secret_key: str, expiration_hours: int = SHARE_LINK_HOURS) -> str:
    """Flask's ``utils.generate_share_token``, byte for byte: base64 of
    ``<params json>|<expiry epoch>|<hmac-sha256 hex>``."""
    params = {"entity_id": str(entity_id), "transaction_date": day.isoformat()}
    expiration = int(datetime.now(UTC).timestamp() + expiration_hours * 3600)
    message = f"{json.dumps(params, sort_keys=True)}|{expiration}"
    signature = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return base64.b64encode(f"{message}|{signature}".encode()).decode("utf-8")


def share_url(path_segment: str) -> str:
    return f"{settings.ACCOUNTS_API_URL}/Minty_Report/{path_segment}/"


def create_share_link(user, entity_id, day: date) -> str:
    if not UserEntity.objects.filter(user_id=user.id, entity_id=entity_id).exists():
        raise ShareRefused("You don't have access to this entity", 403)
    entity = Entity.objects.filter(id=entity_id).first()
    if entity is None:
        raise ShareRefused("Entity not found", 404)
    with transaction.atomic():
        link = ShareLink.objects.select_for_update().filter(entity_id=entity_id, transaction_date=day).first()
        if link is None:
            link = ShareLink(entity_id=entity_id, transaction_date=day)
        if not is_share_path(link.path_segment):
            # A name with no Latin letters has no initials; the prefix is cosmetic.
            link.path_segment = (
                f"{acronym(entity.name) or 'Report'}/{day.strftime('%d_%b_%Y')}/{secrets.token_urlsafe(_SECRET_BYTES)}"
            )
        link.token = share_token(entity_id, day, settings.SECRET_KEY)
        link.expires_at = datetime.now(UTC) + timedelta(hours=SHARE_LINK_HOURS)
        link.save()
    logger.info("share link issued: link=%s entity=%s date=%s user=%s", link.id, entity_id, day, user.id)
    return share_url(link.path_segment)
