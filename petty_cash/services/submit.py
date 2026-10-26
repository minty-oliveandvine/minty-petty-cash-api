"""Submitting the day, the wizard's last step - Flask's ``blueprints/report/services/submit.py``,
ported.

Flask's rules: every step done; the day not in the future (Hong Kong); the expected cash not below
zero; the closing balance = opening + addition + cash sales - expenses - deposit + the difference
(what was counted); the submitter becomes the report's creator and its ``created_at`` (Flask's
``date``) is stamped now; the company's cash in stock follows the submitted count.

As Flask's service changed them on the way out (2026-10-15): nothing is ever deleted; a difference
that no longer matches the count is refused, back to Cash Count (the owner's rule); a difference
needs its reason at submit too; an expense line with no item or no amount goes back to Expenses; one
transaction under a row lock; a report resubmitted after it was in Xero is no longer marked
``xero_integrated`` (``publishing_status`` / ``published_at`` stay, the record that it went there).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from django.db import transaction

from petty_cash.money import cents
from petty_cash.services.cash import NEGATIVE, difference, expected_cash
from petty_cash.services.denominations import refresh_cash_stock
from petty_cash.services.reports import actual_cash_total, future_date_error, last_submitted
from petty_cash.services.sales import cash_sales_from_detail
from shared_models.enums import ReportStatus
from shared_models.models import Report, ReportExpense

logger = logging.getLogger("petty-cash-api")

REQUIRED = ("opening", "sales", "expenses", "deposit", "cash_count")
#: Where each unfinished step is fixed (the Part 15 stage names; Deposit is Cash Count's first block).
STEP_NAMES = {"opening": "Addition to Drawer", "sales": "Sales", "expenses": "Expenses",
              "deposit": "Cash Count", "cash_count": "Cash Count"}
ALREADY_SUBMITTED = "This day is already submitted."


class SubmitRefused(Exception):
    """A submit the rules refuse: the sentence and ``to``, the step that fixes it (``opening`` /
    ``sales`` / ``expenses`` / ``cash_count`` / ``submitted`` / ``dashboard``)."""

    def __init__(self, message: str, to: str):
        super().__init__(message)
        self.message = message
        self.to = to


def counted_now(report: Report) -> tuple[Decimal, Decimal]:
    """``(till + safebox, the difference that count makes against the expected cash NOW)``."""
    till = actual_cash_total(report) or Decimal("0.00")
    safe_box = cents(report.safe_box_balance)
    return cents(till + safe_box), difference(report, till, safe_box)


def closing_at_submit(report: Report) -> Decimal:
    """opening + addition + cash sales (the sale rows) - expenses - deposit + the difference."""
    return cents(
        cents(report.opening_balance) + cents(report.cash_addition) + cash_sales_from_detail(report)
        - cents(report.expense_total) - cents(report.bank_deposit) + cents(report.discrepancy_amount)
    )


def _check(report: Report) -> None:
    if report.status != ReportStatus.DRAFT:
        raise SubmitRefused(ALREADY_SUBMITTED, "submitted")
    # A stale draft (reports.live_drafts): days go in order, so this one would fill a past day.
    last = last_submitted(report.entity_id)
    if last is not None and report.transaction_date < last.transaction_date:
        raise SubmitRefused(
            f"This draft is for {report.transaction_date}, before your last submitted report "
            f"({last.transaction_date}). Days go in order, so it can't be submitted - delete it from "
            "Report History.",
            "dashboard",
        )
    done = set(report.completed_sections or [])
    missing = [s for s in REQUIRED if s not in done]
    if missing:
        first = missing[0]
        raise SubmitRefused(f"Finish {STEP_NAMES[first]} before you submit the day.",
                            "cash_count" if first == "deposit" else first)
    problem = future_date_error(report.transaction_date)
    if problem:
        raise SubmitRefused(problem, "dashboard")
    if expected_cash(report) < 0:
        raise SubmitRefused(NEGATIVE, "cash_count")
    for item, amount in ReportExpense.objects.filter(report_id=report.id).values_list("item", "amount"):
        if not (item or "").strip() or not amount or amount <= 0:
            raise SubmitRefused("An expense has no account or no amount. Fix it in Expenses, then submit.",
                                "expenses")
    _, now = counted_now(report)
    if now != cents(report.discrepancy_amount):
        raise SubmitRefused("The figures changed after the cash count. Check the cash count again, then submit.",
                            "cash_count")
    if now != 0 and not (report.discrepancy_reason or "").strip():
        raise SubmitRefused("The count has a difference with no reason. Add the reason in Cash Count.",
                            "cash_count")


def submit_report(report: Report, user) -> Report:
    """Post the day. ``SubmitRefused`` (nothing written) when a rule says no; any other failure
    rolls the transaction back and is raised - the draft stays as it was."""
    with transaction.atomic():
        locked = Report.objects.select_for_update().get(id=report.id)
        _check(locked)
        now = datetime.now(UTC)
        locked.closing_balance = closing_at_submit(locked)
        locked.created_at = now
        locked.created_by = user.id
        sections = [s for s in (locked.completed_sections or []) if s not in ("ending", "submitted")]
        locked.completed_sections = sections + ["ending", "submitted"]
        locked.current_section = "submitted"
        locked.status = ReportStatus.SUBMITTED
        locked.xero_integrated = False
        locked.submitted_at = now
        locked.save()
        refresh_cash_stock(locked.entity_id)
    logger.info("Report %s submitted: closing %s", locked.id, locked.closing_balance)
    return locked


def submit_problem(report: Report) -> SubmitRefused | None:
    """What would stop the day being submitted right now (the Close page shows it), or None."""
    try:
        _check(report)
    except SubmitRefused as refused:
        return refused
    return None
