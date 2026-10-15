"""Calls to the Flask app (``ACCOUNTS_API_URL``) as a service: each carries a 60-second HS256
assertion signed with the shared ``SECRET_KEY`` whose claims - never the body - name the company
(and the person, where the call acts for one), with a ``scope`` that only one Flask route accepts.

- ``core/xero_tokens.py``: ``POST /api/internal/xero/token``.
- ``forward_deposit_correction``: ``POST /api/internal/xero/deposit-correction`` - the dashboard's
  deposit correction, which can reverse and re-post the deposit in Xero, done by Flask (the owner's
  choice, 2026-10-15: this service holds no Xero code or credentials).
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


def forward_deposit_correction(entity_id, user_id, body) -> tuple[dict, int]:
    """Flask's answer to the correction ``body``, as ``(json, status)`` - the Petty Cash API's own
    shape, handed on unchanged. No usable answer (unreachable, cut off, not the API's JSON) is a 503,
    a timeout a 504 - both saying to check the day, since the correction may have reached Xero."""
    assertion = service_assertion(DEPOSIT_CORRECTION_SCOPE, entity_id, user_id=str(user_id))
    url = f"{settings.ACCOUNTS_API_URL}/api/internal/xero/deposit-correction"
    try:
        resp = requests.post(url, json=body, headers={"Authorization": f"Bearer {assertion}"},
                             timeout=DEPOSIT_CORRECTION_TIMEOUT)
    except requests.Timeout:
        logger.error("Deposit correction: Flask did not answer in time for company %s", entity_id)
        return {"error": UNCONFIRMED}, 504
    except requests.RequestException as exc:
        logger.error("Deposit correction: no answer from Flask for company %s: %s", entity_id, exc)
        return {"error": UNCONFIRMED}, 503
    try:
        answer = resp.json()
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or (resp.status_code >= 400 and "error" not in answer):
        logger.error("Deposit correction: Flask answered %s without the API's JSON for company %s: %s",
                     resp.status_code, entity_id, (resp.text or "")[:200])
        return {"error": UNCONFIRMED}, 503
    if resp.status_code == 401:
        # Our own assertion was refused: a SECRET_KEY that differs between the two services.
        logger.error("Deposit correction: Flask refused this service's assertion for company %s", entity_id)
        return {"error": "Something went wrong on our side. Please try again."}, 500
    return answer, resp.status_code
