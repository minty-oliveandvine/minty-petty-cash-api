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
- ``forward_expense_extract``: ``POST /api/internal/petty-cash/expense-extract`` - a receipt read
  by the model (2026-10-19: this service holds no model code either; Flask's
  ``services/expense_ai.py`` stays the only module that talks to the model).
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings
from minty_shared import tokens

logger = logging.getLogger("petty-cash-api")

DEPOSIT_CORRECTION_SCOPE = "petty-cash-deposit-correction"
#: Flask's gunicorn gives a request 120 s (its Procfile); waiting a little longer means this service
#: never gives up on a correction Flask is still finishing in Xero.
DEPOSIT_CORRECTION_TIMEOUT = (5, 125)
#: Said whenever Flask gave no usable answer: the correction may or may not have reached Xero.
UNCONFIRMED = "I couldn't confirm the deposit correction. Check the day on the dashboard before trying again."


def service_assertion(scope: str, entity_id, **claims) -> str:
    """A 60 s assertion for one of Flask's internal routes (``minty_shared.tokens``)."""
    return tokens.service_assertion(settings.SECRET_KEY, scope, entity_id, **claims)


def assertion_headers(assertion: str) -> dict[str, str]:
    """The headers of every call to Flask: the assertion, and the request's trace, so both
    services' lines for it share one id (``minty_shared.tokens``)."""
    return tokens.assertion_headers(assertion)


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
                             headers=assertion_headers(assertion), timeout=PUBLISH_REASONS_TIMEOUT)
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


EXPENSE_EXTRACT_SCOPE = "petty-cash-expense-extract"
#: The model is given 25 s (Flask's ``EXPENSE_AI_TIMEOUT_S``) and may retry once on a rate limit or a
#: server error; past this the person types the line themselves.
EXPENSE_EXTRACT_TIMEOUT = (5, 60)
#: ``services/expense_ai.py``'s reason codes for a call that never got a usable answer.
REASON_TIMEOUT = "timeout"
REASON_PROVIDER_ERROR = "provider_error"


def forward_expense_extract(entity_id, user_id, receipt: bytes, mime: str, name: str) -> tuple[dict, int]:
    """A checked receipt read by the model, done by Flask (``POST /api/internal/petty-cash/
    expense-extract``; ``services/expense_ai.py`` there is the only module that talks to the model):
    multipart ``receipt`` under a 60 s assertion naming the company and the person. Flask's answer
    is the Petty Cash API's own - 200 ``{suggestions, reason}``, or a refusal ``{error}`` - handed on
    unchanged. As every failure of the reader is silent, no usable answer is 200 ``{suggestions:
    null, reason: "timeout" | "provider_error"}`` (logged), never an error the person must act on;
    Flask refusing OUR assertion is a 500, as for every forward."""
    assertion = service_assertion(EXPENSE_EXTRACT_SCOPE, entity_id, user_id=str(user_id))
    url = f"{settings.ACCOUNTS_API_URL}/api/internal/petty-cash/expense-extract"
    try:
        resp = requests.post(url, files={"receipt": (name or "receipt", receipt, mime)},
                             headers=assertion_headers(assertion), timeout=EXPENSE_EXTRACT_TIMEOUT)
    except requests.Timeout:
        logger.error("Expense extract: Flask did not answer in time for company %s", entity_id)
        return {"suggestions": None, "reason": REASON_TIMEOUT}, 200
    except requests.RequestException as exc:
        logger.error("Expense extract: no answer from Flask for company %s: %s", entity_id, exc)
        return {"suggestions": None, "reason": REASON_PROVIDER_ERROR}, 200
    try:
        answer = resp.json()
    except ValueError:
        answer = None
    if resp.status_code == 401:
        # Our own assertion was refused: a SECRET_KEY that differs between the two services.
        logger.error("Expense extract: Flask refused this service's assertion for company %s", entity_id)
        return {"error": "Something went wrong on our side. Please try again."}, 500
    usable = isinstance(answer, dict) and (
        "error" in answer if resp.status_code >= 400
        else resp.status_code == 200 and "suggestions" in answer and "reason" in answer
    )
    if not usable:
        logger.error("Expense extract: Flask answered %s without the API's JSON for company %s",
                     resp.status_code, entity_id)
        return {"suggestions": None, "reason": REASON_PROVIDER_ERROR}, 200
    return answer, resp.status_code
