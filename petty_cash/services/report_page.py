"""Facts the Close stage and the outcome draw beyond the report's own columns - Flask's
``blueprints/report/services/report_page.py::report_facts``: each expense line with its receipt
count, and the audit line (who submitted, when, Xero). Read-only."""

from __future__ import annotations

from petty_cash.days import hk
from shared_models.models import (
    Attachment,
    Report,
    ReportExpense,
    ReportExpenseAttachment,
    User,
    XeroContactSync,
)


def _submitter(report: Report) -> str | None:
    """"First Last" of the report's creator, or None (Flask looked the creator up by username)."""
    if not report.created_by:
        return None
    user = User.objects.filter(id=report.created_by).first()
    if user is None:
        return None
    return " ".join(p for p in ((user.first_name or "").strip(), (user.last_name or "").strip()) if p) or None


def _expense_lines(report_id) -> list[dict]:
    rows = list(ReportExpense.objects.filter(report_id=report_id).order_by("created_at"))
    contacts = dict(XeroContactSync.objects.filter(id__in=[r.contact_id for r in rows if r.contact_id])
                    .values_list("id", "name"))
    links = list(ReportExpenseAttachment.objects.filter(report_expense_id__in=[r.id for r in rows])
                 .values_list("report_expense_id", "attachment_id"))
    stored = set(Attachment.objects.filter(id__in=[a for _, a in links]).exclude(file_path__isnull=True)
                 .exclude(file_path="").values_list("id", flat=True))
    receipts: dict[str, int] = {}
    for line_id, attachment_id in links:
        if attachment_id in stored:
            receipts[line_id] = receipts.get(line_id, 0) + 1
    return [
        {"id": str(r.id), "item": r.item, "contact_name": contacts.get(r.contact_id), "remarks": r.remarks,
         "amount": r.amount or 0, "receipts": receipts.get(r.id, 0)}
        for r in rows
    ]


def report_facts(report: Report) -> dict:
    return {
        "expenses": _expense_lines(report.id),
        "submitted_by": _submitter(report),
        "submitted_at": hk(report.submitted_at),
        "published_at": hk(report.published_at),
        "publishing_status": str(report.publishing_status) if report.publishing_status else None,
    }
