"""Calls to accounts-api (``ACCOUNTS_API_URL``) as a service: each carries a 60-second HS256
assertion signed with the shared ``SECRET_KEY`` whose claims - never the body - name the company,
with a ``scope`` that only one route accepts.

- ``core/xero_tokens.py``: ``POST /api/internal/xero/token`` - a Xero access token.
- ``services/xero_cards.py``: ``POST /api/internal/xero/sync`` - the company's accounts and contacts
  from Xero.

Until Phase 3 of the Flask retirement (2026-11-06) four Petty Cash actions were also forwarded to
Flask from here (the deposit correction, the publish, its reasons and the Xero cards); they are
done in this service now.
"""

from __future__ import annotations

from django.conf import settings
from minty_shared import tokens
from minty_shared.tokens import assertion_headers

__all__ = ["assertion_headers", "service_assertion"]


def service_assertion(scope: str, entity_id, **claims) -> str:
    """The 60-second assertion every backend builds (``minty_shared.tokens``), signed with the
    shared ``SECRET_KEY``. Sent with ``assertion_headers`` (the bearer and the request's trace)."""
    return tokens.service_assertion(settings.SECRET_KEY, scope, entity_id, **claims)
