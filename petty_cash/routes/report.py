"""A report after it is submitted - Flask's ``petty_cash_api/routes/report.py``.

- ``GET /reports/{id}``: the Daily Report - status, the figures, sales by method, the expense lines
  with their receipts, the cash, the audit facts and what this person may do.
- ``POST /reports/{id}/reopen``: back to a draft at Opening (the latest report only).
- ``DELETE /reports/{id}``: the latest report, with its lines, receipts and counts. A report in
  Xero may be deleted (the owner, 2026-10-15) - the app warns, reading ``was_published``.
- ``POST /reports/{id}/share-link``: a read-only link to the day, valid 30 days, served by Flask.

Another company's, an unknown or an unviewable report is 404. This router is mounted LAST
(``petty_cash/api.py``): ``/reports/{id}`` would otherwise answer for ``/reports/opening``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from ninja import Router

from core.exceptions import Refused
from core.policy import Permission, can_delete_report, can_view_report, has_permission
from petty_cash.money import cents, out
from petty_cash.routes.close import _iso, _sales_history
from petty_cash.routes.expenses import lines_payload
from petty_cash.services.cash import expected_cash
from petty_cash.services.expenses import lines
from petty_cash.services.report_actions import (
    ReportActionRefused,
    delete_report,
    newer_exists,
    reopen_report,
    was_published,
)
from petty_cash.services.report_page import cash_summary, report_facts, report_status
from petty_cash.services.reports import currency_code
from petty_cash.services.sales import cash_sales_from_detail, report_methods
from petty_cash.services.share import ShareRefused, create_share_link
from shared_models.enums import ReportStatus
from shared_models.models import Report

router = Router()

NOT_FOUND = "Hmm, I couldn't find that report."


def _report(request, report_id) -> Report:
    """This company's report by id, if the person may see it - else a 404."""
    try:
        report_id = str(uuid.UUID(str(report_id)))
    except ValueError:
        raise Refused(NOT_FOUND, 404) from None
    report = Report.objects.filter(id=report_id).first()
    if (report is None or str(report.entity_id) != str(request.company.id)
            or not can_view_report(request.auth_user, report)):
        raise Refused(NOT_FOUND, 404)
    return report


@router.get("/reports/{report_id}")
def daily_report(request, report_id: str):
    report = _report(request, report_id)
    entity_id, user = request.company.id, request.auth_user
    facts = report_facts(report)
    label, tone = report_status(report)
    counted, cash_position = cash_summary(report)
    expense_lines = list(lines(report.id))
    status = str(report.status)
    latest = not newer_exists(report)
    history, yesterday = _sales_history(entity_id, report.transaction_date, report.total_sales)
    return {
        "report_id": str(report.id),
        "transaction_date": report.transaction_date.isoformat(),
        "currency_code": currency_code(entity_id),
        "company_name": request.company.name,
        "status": status,
        "status_label": label,
        "status_tone": tone,
        "is_latest": latest,
        "submitted_at": _iso(facts["submitted_at"]),
        "submitted_by": facts["submitted_by"],
        "published_at": _iso(facts["published_at"]),
        "publishing_status": facts["publishing_status"],
        "xero_integrated": bool(report.xero_integrated),
        "was_published": was_published(report),
        "kpis": {
            "total_sales": out(report.total_sales),
            "total_expenses": out(report.expense_total),
            "cash_position": out(cash_position if cash_position is not None else report.closing_balance),
        },
        "sales": {
            "methods": [{**m, "amount": out(m["amount"])} for m in report_methods(entity_id, report.id)],
            "cash": out(cash_sales_from_detail(report)),
            "total": out(report.total_sales),
            "yesterday_total": yesterday,
            "history": history,
        },
        "expenses": {
            "lines": lines_payload(expense_lines),
            "total": out(sum((cents(line.amount) for line in expense_lines), Decimal("0.00"))),
        },
        "cash": {
            "opening_balance": out(report.opening_balance),
            "cash_addition": out(report.cash_addition),
            "cash_addition_type": report.cash_addition_type,
            "cash_sales": out(report.cashsale_total),
            "expenses": out(report.expense_total),
            "bank_deposit": out(report.bank_deposit),
            "expected": out(expected_cash(report)),
            "counted": out(counted),
            "safe_box": out(report.safe_box_balance),
            "difference": {"amount": out(report.discrepancy_amount), "kind": str(report.discrepancy_type or "none"),
                           "reason": report.discrepancy_reason or ""},
        },
        "closing_balance": out(report.closing_balance),
        "permissions": {
            "edit": status != "draft" and latest and has_permission(user, Permission.REPORT_EDIT_OWN, entity_id),
            "delete": latest and can_delete_report(user, report),
            "publish": has_permission(user, Permission.REPORT_PUBLISH, entity_id),
            "share": status != "draft",
        },
    }


@router.post("/reports/{report_id}/reopen")
def reopen(request, report_id: str):
    report = _report(request, report_id)
    try:
        reopen_report(report, request.auth_user)
    except ReportActionRefused as refused:
        raise Refused(refused.message, refused.status) from None
    return {"report_id": str(report.id), "transaction_date": report.transaction_date.isoformat(),
            "status": "draft", "next": "opening"}


@router.delete("/reports/{report_id}")
def delete(request, report_id: str):
    report = _report(request, report_id)
    try:
        delete_report(report, request.auth_user)
    except ReportActionRefused as refused:
        raise Refused(refused.message, refused.status) from None
    return {"deleted": str(report.id), "transaction_date": report.transaction_date.isoformat(), "next": "dashboard"}


@router.post("/reports/{report_id}/share-link")
def share_link(request, report_id: str):
    report = _report(request, report_id)
    if report.status == ReportStatus.DRAFT:
        raise Refused("A draft can't be shared. Submit the day first.", 409)
    try:
        url = create_share_link(request.auth_user, report.entity_id, report.transaction_date)
    except ShareRefused as refused:
        raise Refused(refused.message, refused.status) from None
    return {"url": url}
