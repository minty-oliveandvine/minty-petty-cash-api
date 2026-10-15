"""How the Petty Cash API answers a refusal - Flask's ``petty_cash_api/api.py``, byte for byte:

THE BODY IS ``{"error": "<sentence>"}`` - a sentence minty-petty-cash-web shows as it stands -
except ``unauthorized`` (401), which the app answers by going back through Flask's
``/handoff/petty-cash-web`` for a fresh token. Some refusals add a key the app routes on:
``to`` (where to go: ``opening``, ``dashboard``, ...), ``field`` (the input it is about),
``report_id``. A 500 says one generic sentence; the detail goes to the log, never the caller.
"""

from __future__ import annotations

import logging

from ninja.errors import AuthenticationError, HttpError
from ninja.errors import ValidationError as SchemaValidationError

logger = logging.getLogger("petty-cash-api")

#: Flask's ``api.FAILED``.
FAILED = "Something went wrong on our side. Please try again."
UNAUTHORIZED = "unauthorized"


class Refused(Exception):
    """A rule refused the request: ``{"error": message, **extra}`` with ``status``."""

    def __init__(self, message: str, status: int, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra

    def body(self) -> dict:
        return {"error": self.message, **self.extra}


def register_exception_handlers(api):
    @api.exception_handler(AuthenticationError)
    def on_unauthorized(request, exc):
        return api.create_response(request, {"error": UNAUTHORIZED}, status=401)

    @api.exception_handler(Refused)
    def on_refused(request, exc: Refused):
        if exc.status >= 500:
            logger.error("Refused %s on %s: %s", exc.status, request.path, exc.message)
        return api.create_response(request, exc.body(), status=exc.status)

    @api.exception_handler(SchemaValidationError)
    def on_schema_validation(request, exc):
        # The routes read their bodies by hand to answer with Flask's sentences; a schema error
        # here means a path or query parameter ninja itself refused.
        logger.warning("Schema validation error on %s: %s", request.path, exc.errors)
        return api.create_response(
            request, {"error": "Some of those details didn't look right. Mind checking them?"}, status=422
        )

    @api.exception_handler(HttpError)
    def on_http_error(request, exc: HttpError):
        return api.create_response(request, {"error": str(exc)}, status=exc.status_code)

    @api.exception_handler(Exception)
    def on_unhandled(request, exc):
        # Nothing half-written survives: every write is in a ``transaction.atomic`` block, which
        # Django rolls back when the exception leaves it.
        company = getattr(getattr(request, "company", None), "id", None)
        logger.exception("Petty Cash API failure on company %r: %r", company, exc)
        return api.create_response(request, {"error": FAILED}, status=500)
