"""A read-only link to a submitted day - Flask's ``blueprints/report/services/share.py``, ported.
A link is read through the public JSON read ``GET /api/petty-cash/v1/public/share/{path}``
(``routes/public_share.py``, here and in Flask), which the petty-cash web apps' own ``/share/{path}``
page shows; Flask's old page address (``/Minty_Report/{initials}/{date}/{secret}/``) only redirects
there. The row and the token are made exactly as Flask makes them, so either service opens
either's link (``verify_share_token`` is Flask's ``utils.verify_share_token``, byte for byte, with
the shared ``SECRET_KEY``). One row per company-day: asking again renews its token and expiry and
keeps its address. The secret, not the readable prefix, is what grants access, and it is never
logged - only the row id.

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
import time
from datetime import UTC, date, datetime, timedelta

from django.conf import settings
from django.db import transaction

from shared_models.enums import ReportStatus
from shared_models.models import Entity, Report, ShareLink, UserEntity

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
    """The Petty Cash web app's public page for the link - never Flask's (its HTML is gone)."""
    return f"{settings.PETTY_CASH_WEB_URL}/share/{path_segment}"


def create_share_link(user, entity_id, day: date) -> dict:
    """``{url, path, expires_at}``: ``path`` is what the apps' own ``/share/{path}`` page takes;
    ``url`` is that page on ``PETTY_CASH_WEB_URL``."""
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
    return {"url": share_url(link.path_segment), "path": link.path_segment,
            "expires_at": link.expires_at.astimezone(UTC).isoformat()}


def verify_share_token(token, secret_key):
    """Flask's ``utils.verify_share_token``: ``(True, params)`` for a token this pair of services
    signed that has not run out, else ``(False, None)`` - never an exception."""
    try:
        token_data = base64.b64decode(token.encode("utf-8")).decode("utf-8")
        parts = token_data.split("|")
        if len(parts) != 3:
            return False, None

        params_str, expiration_str, signature = parts
        expiration = int(expiration_str)
        if time.time() > expiration:
            return False, None

        message = f"{params_str}|{expiration_str}"
        expected_signature = hmac.new(
            secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(signature, expected_signature):
            return False, None

        params = json.loads(params_str)
        return True, params
    except Exception:
        return False, None


class LinkRefused(Exception):
    """A share link that opens nothing; ``reason`` is which check failed (Flask's
    ``share.ShareRefused.reason``) - for the status and the log, never for the visitor."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def resolve_share(path_segment, remote_addr=None):
    """``(link, entity, report)`` for a live "{initials}/{date}/{secret}" path, else LinkRefused -
    Flask's ``share.resolve_share``, check for check. An expired link and one whose token fails its
    signature are deleted; a token naming another company or day is refused and kept."""
    if not is_share_path(path_segment):
        logger.warning("share link refused: malformed or old-form path, from %s", remote_addr)
        raise LinkRefused("malformed")

    link = ShareLink.objects.filter(path_segment=path_segment.strip("/")).first()
    if link is None:
        logger.warning("share link refused: unknown secret, from %s", remote_addr)
        raise LinkRefused("unknown")

    if datetime.now(UTC) > link.expires_at:
        logger.info("share link refused: expired, link %s removed", link.id)
        link.delete()
        raise LinkRefused("expired")

    if not settings.SECRET_KEY:
        logger.error("share link refused: SECRET_KEY is not set")
        raise LinkRefused("misconfigured")

    is_valid, params = verify_share_token(link.token, settings.SECRET_KEY)
    if not is_valid or not params:
        logger.warning("share link refused: token fails its signature, link %s removed", link.id)
        link.delete()
        raise LinkRefused("bad_token")

    token_entity_id = params.get("entity_id")
    token_transaction_date = params.get("transaction_date")
    if not token_entity_id or not token_transaction_date:
        logger.warning("share link refused: token names no company or day, link %s", link.id)
        raise LinkRefused("bad_token")

    # The signed token must name the same company and day as the row.
    if str(token_entity_id) != str(link.entity_id) or str(token_transaction_date) != str(link.transaction_date):
        logger.warning("share link refused: token does not match link %s", link.id)
        raise LinkRefused("mismatch")

    entity = Entity.objects.filter(id=str(token_entity_id)).first()
    if entity is None:
        logger.warning("share link refused: no company, link %s", link.id)
        raise LinkRefused("no_company")

    report_date = datetime.strptime(token_transaction_date, "%Y-%m-%d").date()
    # Share-link target: SUBMITTED reports only, never a draft.
    report = (Report.objects.filter(entity_id=str(token_entity_id), transaction_date=report_date)
              .exclude(status=ReportStatus.DRAFT).first())
    if report is None:
        logger.warning("share link refused: no submitted report, link %s", link.id)
        raise LinkRefused("no_report")

    logger.info("share link opened: link=%s entity=%s date=%s from %s",
                link.id, token_entity_id, token_transaction_date, remote_addr)
    return link, entity, report
