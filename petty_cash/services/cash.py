"""The report wizard's Bank Deposit and Cash Count - Flask's ``blueprints/report/services/cash.py``,
ported.

Flask's formulas: cash on hand = opening + addition + cash sales - expenses (the stored cash-sales
column, as Flask's gates read it); expected cash = that less the deposit; difference =
(counted + safebox) - expected, negative being short; the deposit writes the closing balance
without the difference (submit adds it); Save & Exit on the count puts the report back on Cash
Count.

The owner's rules of 2026-10-15: a deposit above the cash on hand is refused; ANY difference counts,
a cent included; Save & Next needs a reason when there is a difference (Save & Exit keeps one
without). Counts are whole numbers of 0 or more, a reason at most 300 characters, a report no
longer a draft is refused, and a refused save writes nothing.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from minty_shared import money as shared_money

from petty_cash.money import cents
from petty_cash.services.denominations import denominations_for, save_counts
from petty_cash.services.reports import (
    currency_code,
    currency_places,
    log_history,
    update_draft_progress,
)
from petty_cash.services.sales import cash_sales_from_detail
from shared_models.enums import ReportStatus
from shared_models.models import Report

NEGATIVE = "Hmm, it looks like your cash balance is negative. Could you fix that first?"
NOT_A_DRAFT = "This report isn't a draft anymore, so its cash can't change here."
NO_DENOMINATIONS = (
    "I don't have any cash denominations set up for this entity yet. Please check the entity's country settings."
)
MAX_REASON = 300


class CashRefused(Exception):
    """A save the rules refuse, with the sentence and the input it is about (``field``)."""

    def __init__(self, message: str, *, field: str | None = None):
        super().__init__(message)
        self.message = message
        self.field = field


def _money(report: Report, value: Decimal) -> str:
    """``HKD 1,234.50`` - the company's code, a NON-BREAKING space, the currency's own places
    (owner, 2026-10-21; ``minty_shared.money``). With no currency the number says so, never a
    silent "$": ``155.00 (No currency selected)``. Rounded to the cent first, as every figure
    here is."""
    return shared_money.format_amount(
        currency_code(report.entity_id), cents(value), currency_places(report.entity_id)
    )


def cash_on_hand(report: Report) -> Decimal:
    return cents(
        cents(report.opening_balance) + cents(report.cash_addition) + cents(report.cashsale_total)
        - cents(report.expense_total)
    )


def expected_cash(report: Report) -> Decimal:
    return cents(cash_on_hand(report) - cents(report.bank_deposit))


def difference(report: Report, counted: Decimal, safe_box: Decimal) -> Decimal:
    return cents((counted - expected_cash(report)) + safe_box)


def difference_kind(amount: Decimal) -> str:
    """Any cent counts."""
    if amount < 0:
        return "short"
    if amount > 0:
        return "over"
    return "none"


def _draft_only(report: Report) -> None:
    if report.status != ReportStatus.DRAFT:
        raise CashRefused(NOT_A_DRAFT)


def _apply_deposit(report: Report, user, amount, action_type: str) -> Decimal:
    _draft_only(report)
    on_hand = cash_on_hand(report)
    if on_hand < 0:
        raise CashRefused(NEGATIVE)
    amount = cents(amount)
    if amount < 0:
        raise CashRefused("Enter the deposit as an amount of 0 or more.", field="bank_deposit")
    if amount > on_hand:
        raise CashRefused(
            f"The deposit can't be more than the cash on hand. Enter {_money(report, on_hand)} or less.",
            field="bank_deposit",
        )
    report.created_by = user.id
    report.bank_deposit = amount
    report.closing_balance = (
        cents(report.opening_balance) + cents(report.cash_addition) + cash_sales_from_detail(report)
        - cents(report.expense_total) - amount
    )
    if action_type == "save_next":
        update_draft_progress(report, action_type, "deposit", "cash_count")
    return amount


def _apply_count(report: Report, user, counts: dict, safe_box, reason: str, action_type: str) -> Decimal:
    _draft_only(report)
    if expected_cash(report) < 0:
        raise CashRefused(NEGATIVE)
    denominations = denominations_for(report.entity_id)
    if not denominations:
        raise CashRefused(NO_DENOMINATIONS)
    safe_box = cents(safe_box)
    if safe_box < 0:
        raise CashRefused("Enter the safebox cash as an amount of 0 or more.", field="safe_box_balance")
    reason = (reason or "").strip()
    if len(reason) > MAX_REASON:
        raise CashRefused(f"Keep the reason to {MAX_REASON} characters or fewer.", field="discrepancy_reason")

    quantities, counted = {}, Decimal("0.00")
    for d in denominations:
        quantity = counts.get(str(d.cash_id), 0) or 0
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            raise CashRefused("Enter each count as a whole number of 0 or more.", field=str(d.cash_id))
        quantities[d.cash_id] = quantity
        counted += d.value * quantity
    counted = cents(counted)

    amount = difference(report, counted, safe_box)
    kind = difference_kind(amount)
    if action_type == "save_next" and kind != "none" and not reason:
        word = "short" if kind == "short" else "over"
        raise CashRefused(f"The count is {_money(report, abs(amount))} {word}. Add the reason to continue.",
                          field="discrepancy_reason")

    report.created_by = user.id
    report.discrepancy_amount = amount if kind != "none" else Decimal("0.00")
    report.discrepancy_type = kind
    report.discrepancy_reason = reason
    report.safe_box_balance = safe_box
    save_counts(report.id, quantities)
    if action_type == "save_next":
        update_draft_progress(report, action_type, "cash_count", "ending")
    else:
        report.current_section = "cash_count"
    return cents(counted + safe_box)


def _log_deposit(report, user, amount):
    log_history(report, user, action="updated", field_changed="bank_deposit", old_value="previous_deposit",
                new_value=f"Bank Deposit: ${float(amount)}")


def _log_count(report, user, total):
    log_history(report, user, action="added", field_changed="cash_count", old_value="previous_cash_count",
                new_value=f"Cash Count: ${float(total)}")


def save_deposit(report: Report, user, amount, action_type: str) -> Report:
    with transaction.atomic():
        locked = Report.objects.select_for_update().get(id=report.id)
        stored = _apply_deposit(locked, user, amount, action_type)
        locked.save()
        _log_deposit(locked, user, stored)
    return locked


def save_cash_count(report: Report, user, counts: dict, safe_box, reason: str, action_type: str) -> Report:
    with transaction.atomic():
        locked = Report.objects.select_for_update().get(id=report.id)
        total = _apply_count(locked, user, counts, safe_box, reason, action_type)
        locked.save()
        _log_count(locked, user, total)
    return locked


def save_deposit_and_count(report: Report, user, deposit, counts: dict, safe_box, reason: str,
                           action_type: str) -> Report:
    """Part 15's Cash Count stage: the deposit, then the count, in ONE transaction - a refusal of
    either writes nothing. Save & Next marks both done."""
    with transaction.atomic():
        locked = Report.objects.select_for_update().get(id=report.id)
        stored = _apply_deposit(locked, user, deposit, action_type)
        total = _apply_count(locked, user, counts, safe_box, reason, action_type)
        locked.save()
        _log_deposit(locked, user, stored)
        _log_count(locked, user, total)
    return locked
