"""Calls to the Flask app (``ACCOUNTS_API_URL``) as a service: each carries a 60-second HS256
assertion signed with the shared ``SECRET_KEY`` whose claims - never the body - name the company
(and the person, where the call acts for one), with a ``scope`` that only one Flask route accepts.

- ``core/xero_tokens.py``: ``POST /api/internal/xero/token``.
- ``forward_deposit_correction``: ``POST /api/internal/xero/deposit-correction`` - the dashboard's
  deposit correction, which can reverse and re-post the deposit in Xero, done by Flask (the owner's
  choice, 2026-10-15: this service holds no Xero code or credentials).
- ``publish_reasons``: ``POST /api/internal/xero/publish-reasons`` - why the history page's failed
  reports didn't publish (the owner's choice, 2026-10-16).
- ``forward_publish``: ``POST /api/internal/xero/publish`` - start a report's Xero publish (the
  same choice: the publish asks Xero and starts Flask's background worker).
- ``forward_xero_settings``: ``POST /api/internal/xero/petty-cash-settings`` - Petty Cash
  Settings' Xero cards (the same choice: each checks the connection live and refreshes from Xero).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import jwt
import requests
from django.conf import settings

logger = logging.getLogger("petty-cash-api")

DEPOSIT_CORRECTION_SCOPE = "petty-cash-deposit-correction"
#: Flask's gunicorn gives a request 120 s (its Procfile); waiting a little longer means this service
#: never gives up on a correction Flask is still finishing in Xero.
DEPOSIT_CORRECTION_TIMEOUT = (5, 125)
#: Said whenever Flask gave no usable answer: the correction may or may not have reached Xero.
UNCONFIRMED = "I couldn't confirm the deposit correction. Check the day on the dashboard before trying again."


def service_assertion(scope: str, entity_id, **claims) -> str:
    now = datetime.now(UTC)
    payload = {"scope": scope, "entity_id": str(entity_id), "iat": now, "exp": now + timedelta(seconds=60), **claims}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def _forward(what: str, path: str, scope: str, timeout, unconfirmed: str, entity_id, user_id, body) -> tuple[dict, int]:
    """Flask's answer to ``body`` at ``path``, as ``(json, status)`` - the Petty Cash API's own
    shape, handed on unchanged. No usable answer (unreachable, cut off, not the API's JSON) is a 503,
    a timeout a 504 - both saying ``unconfirmed``, since the action may have reached Xero."""
    assertion = service_assertion(scope, entity_id, user_id=str(user_id))
    url = f"{settings.ACCOUNTS_API_URL}{path}"
    try:
        resp = requests.post(url, json=body, headers={"Authorization": f"Bearer {assertion}"}, timeout=timeout)
    except requests.Timeout:
        logger.error("%s: Flask did not answer in time for company %s", what, entity_id)
        return {"error": unconfirmed}, 504
    except requests.RequestException as exc:
        logger.error("%s: no answer from Flask for company %s: %s", what, entity_id, exc)
        return {"error": unconfirmed}, 503
    try:
        answer = resp.json()
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or (resp.status_code >= 400 and "error" not in answer):
        logger.error("%s: Flask answered %s without the API's JSON for company %s: %s",
                     what, resp.status_code, entity_id, (resp.text or "")[:200])
        return {"error": unconfirmed}, 503
    if resp.status_code == 401:
        # Our own assertion was refused: a SECRET_KEY that differs between the two services.
        logger.error("%s: Flask refused this service's assertion for company %s", what, entity_id)
        return {"error": "Something went wrong on our side. Please try again."}, 500
    return answer, resp.status_code


def forward_deposit_correction(entity_id, user_id, body) -> tuple[dict, int]:
    """The dashboard's deposit correction ``body``, done by Flask (``_forward``)."""
    return _forward("Deposit correction", "/api/internal/xero/deposit-correction", DEPOSIT_CORRECTION_SCOPE,
                    DEPOSIT_CORRECTION_TIMEOUT, UNCONFIRMED, entity_id, user_id, body)


PUBLISH_SCOPE = "petty-cash-publish"
#: The start answers once the background worker is running (202), but first asks Xero up to three
#: times (the token, a status re-sync, the system-account check). Cut off sooner and the person is
#: told to check the status - the publish may have started.
PUBLISH_TIMEOUT = (5, 60)
#: Said whenever Flask gave no usable answer: the publish may or may not have started.
PUBLISH_UNCONFIRMED = "I couldn't confirm the publish started. Check the report's status before trying again."


def forward_publish(entity_id, user_id, report_id) -> tuple[dict, int]:
    """Start ``report_id``'s Xero publish for this person, done by Flask (``_forward``): 202
    ``{publishing_status, message}`` or the refusal ``{error[, reconnect]}``."""
    return _forward("Publish", "/api/internal/xero/publish", PUBLISH_SCOPE, PUBLISH_TIMEOUT, PUBLISH_UNCONFIRMED,
                    entity_id, user_id, {"report_id": str(report_id)})


XERO_SETTINGS_SCOPE = "petty-cash-xero-settings"
#: Every action asks Xero: the live check (/connections), then the account and contact syncs (with a
#: token refresh) for the answer's lists - a save answers the refreshed read too.
XERO_SETTINGS_TIMEOUT = (5, 60)
#: Said whenever Flask gave no usable answer: a read changed nothing, a save may have been kept.
XERO_SETTINGS_UNREACHABLE = "I couldn't reach Xero's settings just now. Try again in a moment."
XERO_SETTINGS_UNCONFIRMED = "I couldn't confirm the save. Reload the settings to see what was kept."


def forward_xero_settings(entity_id, user_id, action: str, body) -> tuple[dict, int]:
    """Petty Cash Settings' Xero cards for this person, done by Flask (``_forward``): ``action`` is
    ``read``, ``mapping`` or ``account-codes``, ``body`` the save's JSON (None for a read); the
    answer is Flask's ``xero_settings_answer``."""
    unconfirmed = XERO_SETTINGS_UNREACHABLE if action == "read" else XERO_SETTINGS_UNCONFIRMED
    return _forward(f"Xero settings ({action})", "/api/internal/xero/petty-cash-settings", XERO_SETTINGS_SCOPE,
                    XERO_SETTINGS_TIMEOUT, unconfirmed, entity_id, user_id, {"action": action, "body": body})


PUBLISH_REASONS_SCOPE = "petty-cash-publish-reasons"
PUBLISH_REASONS_TIMEOUT = (5, 60)


class PublishReasonsUnavailable(Exception):
    """Flask could not say why the failed reports didn't publish."""


def publish_reasons(entity_id, report_ids) -> dict[str, list[dict]]:
    """``{report id: [{text, resolved}]}`` from Flask's ``POST /api/internal/xero/publish-reasons``
    (the history page's own reasons; working out "resolved" may ask Xero). Raises
    ``PublishReasonsUnavailable`` rather than answer with no reasons - an empty list would read as
    "nothing to fix"."""
    assertion = service_assertion(PUBLISH_REASONS_SCOPE, entity_id)
    url = f"{settings.ACCOUNTS_API_URL}/api/internal/xero/publish-reasons"
    try:
        resp = requests.post(url, json={"report_ids": [str(i) for i in report_ids]},
                             headers={"Authorization": f"Bearer {assertion}"}, timeout=PUBLISH_REASONS_TIMEOUT)
        body = resp.json() if resp.status_code == 200 else None
    except (requests.RequestException, ValueError) as exc:
        logger.error("Publish reasons: no answer from Flask for company %s: %s", entity_id, exc)
        raise PublishReasonsUnavailable from None
    reasons = body.get("reasons") if isinstance(body, dict) else None
    if not isinstance(reasons, dict):
        logger.error("Publish reasons: Flask answered %s for company %s: %s", resp.status_code, entity_id,
                     (resp.text or "")[:200])
        raise PublishReasonsUnavailable
    return reasons
