"""A report after it is submitted - Flask's ``petty_cash_api/routes/report.py``.

- ``GET /reports/{id}``: the Daily Report - status, the figures, sales by method, the expense lines
  with their receipts, the cash, the audit facts and what this person may do.
- ``POST /reports/{id}/reopen``: back to a draft at Opening (the latest report only).
- ``DELETE /reports/{id}``: the latest report, with its lines, receipts and counts. A report in
  Xero may be deleted (the owner, 2026-10-15) - the app warns, reading ``was_published``.
- ``POST /reports/{id}/share-link``: a read-only link to the day, valid 30 days - ``{url, path,
  expires_at}``; the apps show it at their own ``/share/{path}``, read through the public
  ``GET /api/petty-cash/v1/public/share/{path}`` (``public_share.py``).
- ``PUT /reports/{id}/difference-reason``, ``PUT /reports/{id}/withdrawal`` and
  ``PATCH /reports/{id}/expense-lines/{line id}``: a submitted report's details that move no
  balance (``services/report_edits.py``). Any submitted report may be edited this way, not only the
  latest; each answers the Daily Report again.
- ``POST /reports/{id}/publish``: start the Xero publish. The report and ``REPORT_PUBLISH`` are
  checked here (404/403, no call), then Flask does it (``core/accounts_api.py::forward_publish``,
  the owner's choice: this service holds no Xero code) and its answer comes back unchanged - 202
  ``{publishing_status, message}`` or ``{error}`` plus ``reconnect: true`` when Xero must be
  connected again. No usable answer is a 503/504 saying to check the report's status.
- ``GET /reports/{id}/publishing-status``: how the publish went, from the report's columns; a
  failed publish's ``reasons`` come from Flask (``publish_reasons``, as the history page's do).

Another company's, an unknown or an unviewable report is 404. This router is mounted LAST
(``petty_cash/api.py``): ``/reports/{id}`` would otherwise answer for ``/reports/opening``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.http import JsonResponse
from ninja import Router

from core.accounts_api import PublishReasonsUnavailable, forward_publish, publish_reasons
from core.exceptions import Refused
from core.policy import Permission, can_delete_report, can_view_report, has_permission
from petty_cash.days import hk
from petty_cash.money import cents, out
from petty_cash.routes._input import form_and_files, json_body
from petty_cash.routes.close import _iso, _sales_history, report_totals
from petty_cash.routes.expenses import _company_line, lines_payload
from petty_cash.routes.history import NO_REASONS
from petty_cash.services.cash import expected_cash
from petty_cash.services.expenses import expense_accounts, lines, suppliers
from petty_cash.services.report_actions import (
    ReportActionRefused,
    delete_report,
    newer_exists,
    reopen_report,
    was_published,
)
from petty_cash.services.report_edits import (
    EditRefused,
    edit_difference_reason,
    edit_expense_line,
    edit_withdrawal,
    may_edit_details,
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
    return report_payload(request, _report(request, report_id))


def report_figures(report: Report, entity_id) -> dict:
    """The day's figures: the KPIs, sales by method, the cash and the closing balance - what
    ``GET /reports/{id}`` and the public share read (``public_share.py``) both show."""
    counted, cash_position = cash_summary(report)
    return {
        "kpis": {
            **report_totals(report),
            "cash_position": out(cash_position if cash_position is not None else report.closing_balance),
        },
        "sales": {
            "methods": [{**m, "amount": out(m["amount"])} for m in report_methods(entity_id, report.id)],
            "cash": out(cash_sales_from_detail(report)),
            "total": out(report.total_sales),
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
    }


def report_payload(request, report: Report) -> dict:
    """The Daily Report as ``GET /reports/{id}`` answers it (the edits answer it too)."""
    entity_id, user = request.company.id, request.auth_user
    facts = report_facts(report)
    label, tone = report_status(report)
    figures = report_figures(report, entity_id)
    expense_lines = list(lines(report.id))
    status = str(report.status)
    latest = not newer_exists(report)
    history, yesterday = _sales_history(entity_id, report.transaction_date, report.total_sales)
    edit_details = may_edit_details(report, user)
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
        "kpis": figures["kpis"],
        "sales": {**figures["sales"], "yesterday_total": yesterday, "history": history},
        "expenses": {
            "lines": lines_payload(expense_lines),
            "total": out(sum((cents(line.amount) for line in expense_lines), Decimal("0.00"))),
        },
        "cash": figures["cash"],
        "closing_balance": figures["closing_balance"],
        "permissions": {
            "edit": status != "draft" and latest and has_permission(user, Permission.REPORT_EDIT_OWN, entity_id),
            "delete": latest and can_delete_report(user, report),
            "publish": has_permission(user, Permission.REPORT_PUBLISH, entity_id),
            "share": status != "draft",
            "edit_details": edit_details,
        },
        # what an expense line may be changed to - only when it may be
        "options": {
            "suppliers": [{"id": c.xero_contact_id, "name": c.name} for c in suppliers(entity_id)],
            "accounts": [{"id": a.xero_account_id, "code": a.xero_code or "", "name": a.name}
                         for a in expense_accounts(entity_id)],
        } if edit_details else None,
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
        return create_share_link(request.auth_user, report.entity_id, report.transaction_date)
    except ShareRefused as refused:
        raise Refused(refused.message, refused.status) from None


def _edit(request, report: Report, change) -> dict:
    """Run one of report_edits' changes on this company's report; answer the Daily Report."""
    try:
        change(report)
    except EditRefused as refused:
        raise Refused(refused.message, refused.status) from None
    return report_payload(request, Report.objects.get(id=report.id))


def _body(request) -> dict:
    body = json_body(request)
    return body if isinstance(body, dict) else {}


@router.put("/reports/{report_id}/difference-reason")
def edit_reason(request, report_id: str):
    report, body = _report(request, report_id), _body(request)
    return _edit(request, report, lambda r: edit_difference_reason(r, request.auth_user, body.get("reason")))


@router.put("/reports/{report_id}/withdrawal")
def edit_report_withdrawal(request, report_id: str):
    report, body = _report(request, report_id), _body(request)
    return _edit(request, report, lambda r: edit_withdrawal(r, request.auth_user, body.get("withdrawal_type")))


@router.patch("/reports/{report_id}/expense-lines/{line_id}")
def edit_report_line(request, report_id: str, line_id: str):
    """multipart: ``remarks``, ``supplier_id``, ``account_id`` (each only when it changes) and one
    ``receipt`` that replaces the line's."""
    line, line_report = _company_line(request.company.id, line_id)
    if line is None or str(line_report.id) != str(report_id):
        raise Refused("Hmm, I couldn't find that expense.", 404)
    form, files = form_and_files(request)
    receipts = [f for f in files.getlist("receipt") if f and (f.name or "").strip()]
    if len(receipts) > 1:
        raise Refused("Attach one receipt per expense.", 422)
    report = _report(request, report_id)
    return _edit(request, report, lambda r: edit_expense_line(
        r, line, request.auth_user, remarks=form.get("remarks") if "remarks" in form else None,
        supplier_id=form.get("supplier_id") or None, account_id=form.get("account_id") or None, receipts=receipts,
    ))


@router.post("/reports/{report_id}/publish")
def publish(request, report_id: str):
    report = _report(request, report_id)
    if not has_permission(request.auth_user, Permission.REPORT_PUBLISH, request.company.id):
        raise Refused("You don't have permission to publish reports for this company.", 403)
    answer, status = forward_publish(request.company.id, request.auth_user.id, report.id)
    return JsonResponse(answer, status=status)


@router.get("/reports/{report_id}/publishing-status")
def publishing_status(request, report_id: str):
    report = _report(request, report_id)
    reasons = []
    if report.publishing_status == "failed":
        try:
            reasons = publish_reasons(request.company.id, [report.id]).get(str(report.id), [])
        except PublishReasonsUnavailable:
            raise Refused(NO_REASONS, 503) from None
    label, tone = report_status(report)
    return {
        "report_id": str(report.id),
        "publishing_status": str(report.publishing_status) if report.publishing_status else None,
        "status_label": label,
        "status_tone": tone,
        "xero_integrated": bool(report.xero_integrated),
        "was_published": was_published(report),
        "published_at": _iso(hk(report.published_at)),
        "reasons": [{"text": r["text"], "resolved": bool(r["resolved"])} for r in reasons],
    }
