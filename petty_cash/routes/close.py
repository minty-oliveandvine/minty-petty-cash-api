"""The report wizard's last step - Flask's ``petty_cash_api/routes/close.py``: the day's summary, the
submit and what comes after it.

- ``GET /reports/close?transaction_date=``: the draft's figures as Submit will post them, and
  ``problem``, what would stop the submit now (or null).
- ``POST /reports/submit`` ``{transaction_date}``: posts the day. A rule's refusal is 409
  ``{error, to}`` naming the step that fixes it; nothing is written, and nothing is ever deleted.
- ``GET /reports/{report id}/outcome``: what the outcome page says once the day is submitted.

``REPORT_EDIT_OWN`` for the close and the submit; viewing rights for the outcome.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from ninja import Router

from core.exceptions import Refused
from core.policy import Permission, can_view_report, has_permission
from petty_cash.days import parse_day, today
from petty_cash.money import cents, out
from petty_cash.routes._input import BAD_DATE, json_body, query_day, require_edit
from petty_cash.services.cash import expected_cash
from petty_cash.services.report_actions import was_published
from petty_cash.services.report_page import report_facts
from petty_cash.services.reports import (
    actual_cash_total,
    currency_code,
    draft_payload,
    find_draft,
    submitted_report_for,
)
from petty_cash.services.sales import cash_sales_from_detail, page_methods
from petty_cash.services.submit import (
    ALREADY_SUBMITTED,
    SubmitRefused,
    closing_at_submit,
    submit_problem,
    submit_report,
)
from shared_models.models import Report, SaleInfo

router = Router()

NO_DRAFT = "I don't see a draft yet - let's start with the opening entry."
NOT_FOUND = "Hmm, I couldn't find that report."


def _iso(moment):
    return moment.isoformat() if moment is not None else None


def _sales_history(entity_id, day, today_total) -> tuple[list, float | None]:
    """The 30 days up to ``day`` (the day itself with the draft's own total) and the day before's
    total sales, or None - "Compared to Yesterday" and "Sales History". A report of any status
    counts, as in Flask. One query."""
    first = day - timedelta(days=29)
    by_day = {
        d: out(t)
        for d, t in Report.objects.filter(entity_id=entity_id, transaction_date__gte=first, transaction_date__lt=day)
        .values_list("transaction_date", "total_sales")
    }
    history = [{"date": (first + timedelta(days=i)).isoformat(), "total": by_day.get(first + timedelta(days=i), 0.0)}
               for i in range(29)]
    history.append({"date": day.isoformat(), "total": out(today_total)})
    yesterday = by_day.get(day - timedelta(days=1))
    return history, (yesterday if yesterday else None)


def _draft_or_refusal(entity_id, day) -> Report:
    draft = find_draft(entity_id, day)
    if draft is not None:
        return draft
    done = submitted_report_for(entity_id, day)
    if done is not None:
        raise Refused(ALREADY_SUBMITTED, 409, to="submitted", report_id=str(done.id))
    raise Refused(NO_DRAFT, 409, to="opening")


@router.get("/reports/close")
def close_page(request):
    require_edit(request)
    entity_id = request.company.id
    day = query_day(request, required=True)
    draft = _draft_or_refusal(entity_id, day)

    facts = report_facts(draft)
    methods = [m for m in page_methods(entity_id, draft.id) if m["value_name"] != SaleInfo.CASH_VALUE_NAME]
    lines = facts["expenses"]
    problem = submit_problem(draft)
    history, yesterday = _sales_history(entity_id, day, draft.total_sales)
    return {
        "transaction_date": day.isoformat(),
        "currency_code": currency_code(entity_id),
        "draft": draft_payload(draft),
        "opening_balance": out(draft.opening_balance),
        "cash_addition": out(draft.cash_addition),
        "sales": {
            "methods": [{**m, "amount": out(m["amount"])} for m in methods],
            "cash": out(cash_sales_from_detail(draft)),
            "total": out(draft.total_sales),
            "yesterday_total": yesterday,
            "history": history,
        },
        "expenses": {
            "lines": [{"id": e["id"], "item": e["item"] or "", "supplier": e["contact_name"] or "",
                       "remarks": e["remarks"] or "", "amount": out(e["amount"]), "receipts": e["receipts"]}
                      for e in lines],
            "total": out(sum((cents(e["amount"]) for e in lines), Decimal("0.00"))),
        },
        "cash": {
            "bank_deposit": out(draft.bank_deposit),
            "expected": out(expected_cash(draft)),
            "counted": out(actual_cash_total(draft)),
            "safe_box": out(draft.safe_box_balance),
            "difference": {"amount": out(draft.discrepancy_amount), "kind": str(draft.discrepancy_type or "none"),
                           "reason": draft.discrepancy_reason or ""},
        },
        "closing_balance": out(closing_at_submit(draft)),
        "problem": {"error": problem.message, "to": problem.to} if problem else None,
    }


@router.post("/reports/submit")
def submit_day(request):
    require_edit(request)
    entity_id = request.company.id
    body = json_body(request)
    day = parse_day(body.get("transaction_date")) if isinstance(body, dict) else None
    if day is None:
        raise Refused(BAD_DATE, 422)
    draft = _draft_or_refusal(entity_id, day)
    try:
        posted = submit_report(draft, request.auth_user)
    except SubmitRefused as refused:
        extra = {"report_id": str(draft.id)} if refused.to == "submitted" else {}
        raise Refused(refused.message, 409, to=refused.to, **extra) from None
    return {
        "report_id": str(posted.id),
        "transaction_date": day.isoformat(),
        "status": str(posted.status),
        "submitted_at": _iso(posted.submitted_at),
        "closing_balance": out(posted.closing_balance),
        "next": "submitted",
    }


@router.get("/reports/{report_id}/outcome")
def report_outcome(request, report_id: str):
    entity_id = request.company.id
    try:
        report_id = str(uuid.UUID(str(report_id)))
    except ValueError:
        raise Refused(NOT_FOUND, 404) from None
    report = Report.objects.filter(id=report_id).first()
    if report is None or str(report.entity_id) != str(entity_id) or not can_view_report(request.auth_user, report):
        raise Refused(NOT_FOUND, 404)
    facts = report_facts(report)
    status = str(report.status)
    return {
        "report_id": str(report.id),
        "transaction_date": report.transaction_date.isoformat(),
        "currency_code": currency_code(entity_id),
        "company_name": request.company.name,
        "status": status,
        "submitted": status != "draft",
        "submitted_at": _iso(facts["submitted_at"]),
        "submitted_by": facts["submitted_by"],
        "cash_counted": out((actual_cash_total(report) or Decimal("0.00")) + cents(report.safe_box_balance)),
        "difference": {"amount": out(report.discrepancy_amount), "kind": str(report.discrepancy_type or "none"),
                       "has_reason": bool((report.discrepancy_reason or "").strip())},
        "xero": {"publishing_status": facts["publishing_status"], "published_at": _iso(facts["published_at"]),
                 "xero_integrated": bool(report.xero_integrated),
                 # it has been to Xero, even if changed since: a publish warns (report_actions)
                 "was_published": was_published(report)},
        "can_publish": has_permission(request.auth_user, Permission.REPORT_PUBLISH, entity_id),
        "is_today": report.transaction_date == today(),
        "next_day": (report.transaction_date + timedelta(days=1)).isoformat(),
    }
