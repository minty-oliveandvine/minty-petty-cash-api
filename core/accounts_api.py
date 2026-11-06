"""Calls to the Flask app (``ACCOUNTS_API_URL``) as a service: each carries a 60-second HS256
assertion signed with the shared ``SECRET_KEY`` whose claims - never the body - name the company
(and the person, where the call acts for one), with a ``scope`` that only one Flask route accepts.

- ``core/xero_tokens.py``: ``POST /api/internal/xero/token``.
- ``forward_xero_settings``: ``POST /api/internal/xero/petty-cash-settings`` - Petty Cash
  Settings' Xero cards (the same choice: each checks the connection live and refreshes from Xero).
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings
from minty_shared import tokens
from minty_shared.tokens import assertion_headers

logger = logging.getLogger("petty-cash-api")

def service_assertion(scope: str, entity_id, **claims) -> str:
    """The 60-second assertion every backend builds (``minty_shared.tokens``), signed with the
    shared ``SECRET_KEY``. Sent with ``assertion_headers`` (the bearer and the request's trace)."""
    return tokens.service_assertion(settings.SECRET_KEY, scope, entity_id, **claims)


def _forward(what: str, path: str, scope: str, timeout, unconfirmed: str, entity_id, user_id, body) -> tuple[dict, int]:
    """Flask's answer to ``body`` at ``path``, as ``(json, status)`` - the Petty Cash API's own
    shape, handed on unchanged. No usable answer (unreachable, cut off, not the API's JSON) is a 503,
    a timeout a 504 - both saying ``unconfirmed``, since the action may have reached Xero."""
    assertion = service_assertion(scope, entity_id, user_id=str(user_id))
    url = f"{settings.ACCOUNTS_API_URL}{path}"
    try:
        resp = requests.post(url, json=body, headers=assertion_headers(assertion), timeout=timeout)
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
