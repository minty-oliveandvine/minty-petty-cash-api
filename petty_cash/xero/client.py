"""HTTP to Xero's accounting API for one company: the headers, a timeout on every call, and the
id Xero gives back. Flask's ``publish.py`` helpers (``bank_transfer_to_xero``, ``_parse_xero_id``),
which had no timeout on the transfer post.
"""

from __future__ import annotations

import json
import logging

import requests
from django.conf import settings

logger = logging.getLogger("petty-cash-api")

#: (connect, read): Flask's for every Xero call it timed.
TIMEOUT = (30, 60)


def headers(access_token: str, xero_org_id) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Xero-Tenant-Id": str(xero_org_id),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def get(path: str, access_token: str, xero_org_id, *, params=None) -> requests.Response:
    """``GET {XERO_API_BASE_URL}/{path}``. Raises ``requests.RequestException`` when Xero can't be reached."""
    return requests.get(f"{settings.XERO_API_BASE_URL}/{path}", headers=headers(access_token, xero_org_id),
                        params=params, timeout=TIMEOUT)


def post(path: str, access_token: str, xero_org_id, payload: dict) -> requests.Response:
    """``POST {XERO_API_BASE_URL}/{path}`` with a JSON body. Raises ``requests.RequestException``."""
    return requests.post(f"{settings.XERO_API_BASE_URL}/{path}", headers=headers(access_token, xero_org_id),
                         data=json.dumps(payload), timeout=TIMEOUT)


def parse_id(response_text: str | None, collection: str, id_field: str) -> str:
    """The id of the first object in a Xero success body (``{"Invoices": [{"InvoiceID": ...}]}``),
    or "". Never raises: a publish Xero accepted is not failed over an unreadable id (it only means
    a later republish cannot update that object)."""
    try:
        payload = json.loads(response_text or "")
        return str(payload[collection][0][id_field] or "")
    except (ValueError, TypeError, KeyError, IndexError):
        logger.warning("Xero's answer had no %s; a later republish cannot update this object", id_field)
        return ""
