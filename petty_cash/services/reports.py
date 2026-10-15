"""What every wizard step shares - Flask's ``blueprints/report/services/shared.py`` and the Report
model's computed properties, ported:

- finding the day's draft, the submitted report for a day, the last submitted report;
- the wizard's progress (``update_draft_progress``);
- a history line, written IN THE SAME TRANSACTION as the change it records (Flask's
  ``log_history`` committed on its own and swallowed its errors - here a failure fails the save);
- the counted cash (``Report.actual_cash_total``), the onboarding defaults, the future-date rule;
- the draft as every step's page reads it (``draft_payload``) and the company's currency code.
"""

from __future__ import annotations

from decimal import Decimal

from django.db.models import F, Sum

from petty_cash.days import today
from petty_cash.money import cents
from shared_models.enums import ReportStatus
from shared_models.models import (
    CurrencyInfo,
    Entity,
    EntityPettycashSettings,
    Report,
    ReportCashCount,
    ReportHistory,
)


def find_draft(entity_id, day, draft_id=None) -> Report | None:
    """The draft a save writes to: the one named, else the one for (company, day)."""
    drafts = Report.objects.filter(entity_id=entity_id, status=ReportStatus.DRAFT)
    if draft_id:
        return drafts.filter(id=draft_id).first()
    return drafts.filter(transaction_date=day).first()


def submitted_report_for(entity_id, day) -> Report | None:
    return Report.objects.filter(entity_id=entity_id, transaction_date=day).exclude(status=ReportStatus.DRAFT).first()


def last_submitted(entity_id, before=None) -> Report | None:
    reports = Report.objects.filter(entity_id=entity_id).exclude(status=ReportStatus.DRAFT)
    if before is not None:
        reports = reports.filter(transaction_date__lt=before)
    return reports.order_by("-transaction_date").first()


def previous_report(entity_id, before) -> Report | None:
    """The report just before ``before``, WHATEVER its status."""
    return Report.objects.filter(entity_id=entity_id, transaction_date__lt=before).order_by("-transaction_date").first()


def update_draft_progress(report: Report, action_type: str, current_section: str, next_section: str) -> None:
    """Flask's ``update_draft_progress``: Save & Next moves the report on to ``next_section``,
    Save & Exit keeps it on ``current_section``; either marks ``current_section`` done. Anything
    else changes nothing."""
    completed = list(report.completed_sections or [])
    if action_type == "save_next":
        report.current_section = next_section
    elif action_type == "save_exit":
        report.current_section = current_section
    else:
        return
    if current_section not in completed:
        completed.append(current_section)
    report.completed_sections = completed


def log_history(report: Report, user, *, action: str, field_changed: str, old_value=None, new_value=None) -> None:
    ReportHistory.objects.create(
        report_id=report.id,
        user_id=getattr(user, "id", None),
        action=action,
        field_changed=field_changed,
        old_value=None if old_value is None else str(old_value),
        new_value=None if new_value is None else str(new_value),
    )


def actual_cash_total(report: Report) -> Decimal | None:
    """What the cash count added up to; 0 when the count step is done with nothing counted (a zero
    count stores no row); None when never counted."""
    total = ReportCashCount.objects.filter(report_id=report.id).aggregate(
        total=Sum(F("quantity") * F("cash_value"))
    )["total"]
    if total is not None:
        return cents(total)
    if "cash_count" in (report.completed_sections or []):
        return Decimal("0.00")
    return None


def first_report_defaults(entity_id) -> tuple[Decimal, object]:
    """``(opening_balance, start_date)`` for the company's FIRST report, from onboarding's settings
    row; ``(0, None)`` without one."""
    row = EntityPettycashSettings.objects.filter(entity_id=entity_id).only("opening_balance", "start_date").first()
    if row is None:
        return Decimal("0.00"), None
    return cents(row.opening_balance), row.start_date


def future_date_error(day, today_=None) -> str | None:
    """No report may be dated after Hong Kong's today."""
    today_ = today_ or today()
    if day and day > today_:
        return (
            "Transaction date cannot be in the future. You can only create "
            f"reports for dates up to {today_}."
        )
    return None


def currency_code(entity_id) -> str | None:
    """The company's ISO currency code - every amount on the pages is written with it."""
    currency_id = Entity.objects.filter(id=entity_id).values_list("currency_id", flat=True).first()
    if not currency_id:
        return None
    code = CurrencyInfo.objects.filter(id=currency_id).values_list("currency_code", flat=True).first()
    return code.strip() if code else None


def draft_payload(report: Report | None) -> dict | None:
    """The day's report as every step's page reads it (its badge and its stepper)."""
    if report is None:
        return None
    return {
        "id": str(report.id),
        "status": str(report.status),
        "current_section": report.current_section,
        "completed_sections": list(report.completed_sections or []),
        "xero_integrated": bool(report.xero_integrated),
        "publishing_status": str(report.publishing_status) if report.publishing_status else None,
    }


def progress_payload(report: Report, next_step: str) -> dict:
    """What every step's save answers."""
    return {
        "draft_id": str(report.id),
        "transaction_date": report.transaction_date.isoformat(),
        "current_section": report.current_section,
        "completed_sections": list(report.completed_sections or []),
        "next": next_step,
    }
