"""``GET /api/petty-cash/v1/public/share/{initials}/{date}/{secret}``: the read-only Daily Report a
share link shows, for the petty-cash web apps' public ``/share/{path}`` page - Flask's
``petty_cash_api/routes/public_share.py``, same path, JSON, statuses and headers, read from the
database alone.

UNAUTHENTICATED: the ONE operation with ``auth=None`` (the API's default is the bearer door,
``config/urls.py``); ``petty_cash/tests/test_public_share.py`` pins that it is the only one besides
the OpenAPI document. It answers only what the shared page shows: the company's name, the day, the
status pill, the KPIs, sales by method, the expense lines (item, supplier, amount, receipt names -
no description, no receipt id or file access), the cash with the difference and its reason, the
closing balance, who submitted it and when, and when the link expires.

``services/share.py::resolve_share`` decides, as Flask's does. Every refusal reads the same - 404
with one sentence - except an expired link, 410 (its row is deleted). The secret is in the address,
so the answer is ``no-store`` and ``no-referrer``, and the secret is never logged: the request log
blanks the path (``core/middleware.py``), Django's own "Not Found: <path>" warning is skipped, and a
failure logs its type and frames, never its message (a database error's parameters).

No rate limit, as in Flask (no limiter in either service, no new dependency).
"""

from __future__ import annotations

import logging
import traceback
from datetime import UTC
from decimal import Decimal

from django.db import transaction
from django.http import JsonResponse
from ninja import Router

from core.exceptions import FAILED
from petty_cash.money import cents, out
from petty_cash.routes.close import _iso
from petty_cash.routes.expenses import lines_payload
from petty_cash.routes.report import report_figures
from petty_cash.services.expenses import lines
from petty_cash.services.report_page import report_facts, report_status
from petty_cash.services.reports import currency_code
from petty_cash.services.share import LinkRefused, resolve_share

logger = logging.getLogger("petty-cash-api")

router = Router()

#: Where the public reads live, beside ``/companies/{entity_id}`` (``petty_cash/api.py``).
PREFIX = "/public"
GONE = "This link doesn't work. Ask the person who shared it for a new link."
EXPIRED = "This link has expired. Ask the person who shared it for a new link."


def _answer(body: dict, status: int = 200) -> JsonResponse:
    resp = JsonResponse(body, status=status)
    resp["Cache-Control"] = "no-store"
    resp["Referrer-Policy"] = "no-referrer"
    # Django logs every 4xx/5xx as "<reason>: <path>" (django.request); the path holds the secret.
    resp._has_been_logged = True
    return resp


@router.get("/share/{path:share_path}", auth=None, include_in_schema=True)
def shared_report(request, share_path: str):
    try:
        with transaction.atomic():
            try:
                link, entity, report = resolve_share(share_path, request.META.get("REMOTE_ADDR"))
            except LinkRefused as refused:
                if refused.reason == "expired":
                    return _answer({"error": EXPIRED}, 410)
                if refused.reason == "misconfigured":
                    return _answer({"error": FAILED}, 500)
                return _answer({"error": GONE}, 404)
            return _answer(shared_payload(link, entity, report))
    except Exception as exc:  # noqa: BLE001 - logged below without its message, answered generically
        frames = "".join(traceback.format_tb(exc.__traceback__))
        logger.error("Petty Cash public share failure: %s\n%s", type(exc).__name__, frames)
        return _answer({"error": FAILED}, 500)


def shared_payload(link, entity, report) -> dict:
    entity_id = str(entity.id)
    facts = report_facts(report)
    label, tone = report_status(report)
    figures = report_figures(report, entity_id)
    expense_lines = list(lines(report.id))
    return {
        "company_name": entity.name,
        "transaction_date": report.transaction_date.isoformat(),
        "currency_code": currency_code(entity_id),
        "status": str(report.status),
        "status_label": label,
        "status_tone": tone,
        "publishing_status": facts["publishing_status"],
        "submitted_at": _iso(facts["submitted_at"]),
        "submitted_by": facts["submitted_by"],
        "kpis": figures["kpis"],
        # sale_id is the company's own row: the page needs the name, kind and amount alone
        "sales": {**figures["sales"], "methods": [
            {"name": m["name"], "value_name": m["value_name"], "type": m["type"], "amount": m["amount"]}
            for m in figures["sales"]["methods"]]},
        "expenses": {
            "lines": [{"item": line["item"], "supplier_name": line["supplier"]["name"] if line["supplier"] else None,
                       "amount": line["amount"], "receipts": [{"name": r["name"]} for r in line["receipts"]]}
                      for line in lines_payload(expense_lines)],
            "total": out(sum((cents(line.amount) for line in expense_lines), Decimal("0.00"))),
        },
        "cash": figures["cash"],
        "closing_balance": figures["closing_balance"],
        "expires_at": link.expires_at.astimezone(UTC).isoformat(),
    }
