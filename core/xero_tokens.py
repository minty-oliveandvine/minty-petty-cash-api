"""A Xero access token for one company, from the Flask app - the only service that calls Xero's
``/connect/token`` (Xero rotates the refresh token on every use, so a second refresher would break
the connection). Flask's ``POST /api/internal/xero/token`` takes a 60-second HS256 assertion whose
claims carry the company, refreshes behind its lock and answers ``{access_token, xero_org_id}``;
409 means the connection needs a person to reconnect it.

minty-payment-request-api's ``bills/services/xero_token_service.py`` reads ``user_token`` first;
this service never reads Xero credentials at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
from django.conf import settings

from core.accounts_api import service_assertion

logger = logging.getLogger("petty-cash-api")

SCOPE = "xero-access-token"


class XeroTokenUnavailable(Exception):
    """No token. ``reconnect`` is True when Flask said the connection needs a person (409), False
    when Flask could not be asked or answered something else."""

    def __init__(self, reconnect: bool):
        super().__init__("reconnect required" if reconnect else "token service failed")
        self.reconnect = reconnect


@dataclass(frozen=True)
class XeroToken:
    access_token: str
    xero_org_id: str | None


def xero_token(entity_id) -> XeroToken:
    assertion = service_assertion(SCOPE, entity_id)
    url = f"{settings.ACCOUNTS_API_URL}/api/internal/xero/token"
    try:
        resp = requests.post(url, headers={"Authorization": f"Bearer {assertion}"}, timeout=settings.XERO_TOKEN_TIMEOUT)
    except requests.RequestException as exc:
        logger.error("Xero token service unreachable for company %s: %s", entity_id, exc)
        raise XeroTokenUnavailable(reconnect=False) from None
    if resp.status_code == 409:
        logger.info("Xero token service: reconnect required for company %s", entity_id)
        raise XeroTokenUnavailable(reconnect=True)
    if resp.status_code != 200:
        logger.error("Xero token service answered %s for company %s: %s", resp.status_code, entity_id,
                     (resp.text or "")[:200])
        raise XeroTokenUnavailable(reconnect=False)
    try:
        body = resp.json() or {}
    except ValueError:
        body = {}
    if not body.get("access_token"):
        logger.error("Xero token service gave no access_token for company %s", entity_id)
        raise XeroTokenUnavailable(reconnect=False)
    return XeroToken(access_token=body["access_token"], xero_org_id=body.get("xero_org_id"))
