"""The report wizard's Bank Deposit and Cash Count steps - Flask's ``petty_cash_api/routes/cash.py``.

- ``GET /reports/deposit?transaction_date=``: the day's draft, the figures the cash on hand is made
  of, the cash on hand and the saved deposit.
- ``PUT /reports/deposit`` ``{transaction_date, bank_deposit, action}``.
- ``GET /reports/cash-count?transaction_date=``: the figures, the expected cash, the company's
  denominations with the saved counts, the safebox and the saved difference.
- ``PUT /reports/cash-count`` ``{transaction_date, counts: {<denomination id>: n}, safe_box_balance,
  discrepancy_reason, action, bank_deposit?}``: the count; with ``bank_deposit`` the deposit is saved
  first in the same transaction (Part 15's Cash Count stage).

``REPORT_EDIT_OWN``. No draft for the day is 409 ``to: "opening"``; a rule's refusal about one input
is 422 ``{error, field}``; one no input can fix (a negative cash on hand, a report no longer a draft)
is 409 ``{error}``.
"""

from __future__ import annotations

from ninja import Router

from core.exceptions import Refused
from petty_cash.days import parse_day
from petty_cash.money import out
from petty_cash.routes._input import (
    BAD_DATE,
    action_type,
    amount,
    json_body,
    query_day,
    require_edit,
)
from petty_cash.services.cash import (
    CashRefused,
    cash_on_hand,
    expected_cash,
    save_cash_count,
    save_deposit,
    save_deposit_and_count,
)
from petty_cash.services.denominations import denomination_rows, denominations_for
from petty_cash.services.reports import currency_code, draft_payload, find_draft, progress_payload

router = Router()

NO_DRAFT = "I don't see a draft yet - let's start with the opening entry."
BAD_ACTION = "Choose Save & Next or Save & Exit."
BAD_DEPOSIT = "Enter the deposit as an amount of 0 or more."
MAX_COUNT = 1_000_000


def _figures(report) -> dict:
    """What the cash on hand and the expected cash are made of, as the pages show them."""
    return {
        "opening_balance": out(report.opening_balance),
        "cash_addition": out(report.cash_addition),
        "cash_sales": out(report.cashsale_total),
        "expenses": out(report.expense_total),
        "bank_deposit": out(report.bank_deposit),
        "cash_on_hand": out(cash_on_hand(report)),
        "expected_cash": out(expected_cash(report)),
    }


def _draft_for(entity_id, day):
    draft = find_draft(entity_id, day)
    if draft is None:
        raise Refused(NO_DRAFT, 409, to="opening")
    return draft


def _refused(refused: CashRefused) -> Refused:
    if refused.field:
        return Refused(refused.message, 422, field=refused.field)
    return Refused(refused.message, 409)


def _body(request, what: str) -> dict:
    body = json_body(request)
    if not isinstance(body, dict):
        raise Refused(f"Send the {what} as JSON.", 422)
    return body


# --- Deposit -----------------------------------------------------------------------------------------


@router.get("/reports/deposit")
def deposit_page(request):
    require_edit(request)
    entity_id = request.company.id
    day = query_day(request, required=True)
    draft = _draft_for(entity_id, day)
    return {"transaction_date": day.isoformat(), "currency_code": currency_code(entity_id),
            "draft": draft_payload(draft), **_figures(draft)}


@router.put("/reports/deposit")
def save_deposit_step(request):
    require_edit(request)
    entity_id = request.company.id
    body = _body(request, "deposit")
    day = parse_day(body.get("transaction_date"))
    if day is None:
        raise Refused(BAD_DATE, 422)
    raw = body.get("bank_deposit")
    deposit = amount(0 if raw is None else raw)
    if deposit is None:
        raise Refused(BAD_DEPOSIT, 422, field="bank_deposit")
    action = action_type(body)
    if action is None:
        raise Refused(BAD_ACTION, 422)
    draft = _draft_for(entity_id, day)
    try:
        draft = save_deposit(draft, request.auth_user, deposit, action)
    except CashRefused as refused:
        raise _refused(refused) from None
    return progress_payload(draft, "cash_count" if action == "save_next" else "dashboard")


# --- Cash Count --------------------------------------------------------------------------------------


@router.get("/reports/cash-count")
def cash_count_page(request):
    require_edit(request)
    entity_id = request.company.id
    day = query_day(request, required=True)
    draft = _draft_for(entity_id, day)
    return {
        "transaction_date": day.isoformat(),
        "currency_code": currency_code(entity_id),
        "draft": draft_payload(draft),
        **_figures(draft),
        "denominations": [
            {"id": str(row["cash_id"]), "value": row["value"], "kind": str(row["type"] or ""),
             "name": row["name"], "count": int(row["count"] or 0)}
            for row in denomination_rows(entity_id, draft.id)
        ],
        "safe_box_balance": out(draft.safe_box_balance),
        "discrepancy": {
            "amount": out(draft.discrepancy_amount),
            "kind": str(draft.discrepancy_type or "none"),
            "reason": draft.discrepancy_reason or "",
        },
    }


@router.put("/reports/cash-count")
def save_cash_count_step(request):
    require_edit(request)
    entity_id = request.company.id
    body = _body(request, "count")
    day = parse_day(body.get("transaction_date"))
    if day is None:
        raise Refused(BAD_DATE, 422)
    action = action_type(body)
    if action is None:
        raise Refused(BAD_ACTION, 422)
    raw_counts = body.get("counts") or {}
    if not isinstance(raw_counts, dict):
        raise Refused("Send the counts as one number per denomination.", 422)
    known = {str(d.cash_id) for d in denominations_for(entity_id)}
    counts = {}
    for cash_id, quantity in raw_counts.items():
        if cash_id not in known:
            raise Refused("One of those denominations isn't used by this company. Reload the page to see them.", 422)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 0 <= quantity <= MAX_COUNT:
            raise Refused("Enter each count as a whole number of 0 or more.", 422, field=cash_id)
        counts[cash_id] = quantity
    raw_safe = body.get("safe_box_balance")
    safe_box = amount(0 if raw_safe is None else raw_safe)
    if safe_box is None:
        raise Refused("Enter the safebox cash as an amount of 0 or more.", 422, field="safe_box_balance")
    reason = body.get("discrepancy_reason") or ""
    if not isinstance(reason, str):
        raise Refused("Write the reason as text.", 422, field="discrepancy_reason")
    deposit = None
    if body.get("bank_deposit") is not None:
        deposit = amount(body["bank_deposit"])
        if deposit is None:
            raise Refused(BAD_DEPOSIT, 422, field="bank_deposit")

    draft = _draft_for(entity_id, day)
    try:
        if deposit is None:
            draft = save_cash_count(draft, request.auth_user, counts, safe_box, reason, action)
        else:
            draft = save_deposit_and_count(draft, request.auth_user, deposit, counts, safe_box, reason, action)
    except CashRefused as refused:
        raise _refused(refused) from None
    answer = progress_payload(draft, "ending" if action == "save_next" else "dashboard")
    answer["discrepancy"] = {"amount": out(draft.discrepancy_amount), "kind": str(draft.discrepancy_type or "none")}
    return answer
