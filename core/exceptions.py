"""How the Petty Cash API answers a refusal: the one envelope every Minty API answers with
(``minty_shared.django.errors``) - ``{"error": "<sentence>"}``, a sentence minty-petty-cash-web
shows as it stands. Some refusals add a key the app routes on: ``to`` (where to go: ``opening``,
``dashboard``, ...), ``field`` (the input it is about - also given as ``fields``, the envelope's
per-input map), ``report_id``. A 401 is ``{"error": "Unauthorized"}``, which the app answers by
going back through Flask's ``/handoff/petty-cash-web`` for a fresh token. A 500 says one house
sentence; the detail goes to the log, never the caller.
"""

from __future__ import annotations

import logging

from minty_shared.django.errors import HOUSE_FALLBACK, ApiRefusal, register_error_handlers

logger = logging.getLogger("petty-cash-api")

#: The one sentence for a failure this service did not foresee (every API's house sentence).
FAILED = HOUSE_FALLBACK


class Refused(ApiRefusal):
    """A rule refused the request: ``{"error": message, **extra}`` with ``status``; a ``field``
    extra also lands in ``fields``."""

    def __init__(self, message: str, status: int, **extra):
        field = extra.get("field")
        super().__init__(message, status, fields={field: message} if field else None, **extra)


def register_exception_handlers(api):
    register_error_handlers(api, logger=logger)
