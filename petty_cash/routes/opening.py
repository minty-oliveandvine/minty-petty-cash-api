"""The report wizard's Opening step - Flask's ``petty_cash_api/routes/opening.py``.

- ``GET /reports/opening?transaction_date=``: the day's draft, or what the day opens from.
- ``PUT /reports/opening`` ``{transaction_date, cash_addition, withdrawal, action}``: the save. The
  opening balance is the server's own "Starting Cash Balance" (a first report's: the onboarding
  balance) - never taken from the client.

A date rule's refusal is 409 ``{error, to: "dashboard"}``; a negative balance 409
``{error, to: "opening"}``; bad input 422. ``REPORT_EDIT_OWN`` throughout.
"""

from __future__ import annotations

from ninja import Router

from core.exceptions import Refused
from petty_cash.days import parse_day, today
from petty_cash.money import out
from petty_cash.routes._input import (
    BAD_DATE,
    action_type,
    amount,
    json_body,
    query_day,
    require_edit,
)
from petty_cash.services.opening import (
    NEGATIVE,
    OpeningRefused,
    first_report_opening,
    new_report_date_problem,
    opening_state,
    save_opening,
    starting_cash_balance,
)
from petty_cash.services.reports import (
    currency_code,
    draft_payload,
    progress_payload,
    submitted_report_for,
)

router = Router()

# Owner 2026-10-21: never a silent "$" - a company with no currency picks one before any report.
NO_CURRENCY = "No currency is selected for this company. Choose one in Petty Cash Settings to start a report."


def opening_payload(entity_id, selected) -> dict:
    state = opening_state(entity_id, selected)
    draft = state["draft"]
    day = selected or state["next_transaction_date"] or today()
    cash_addition = state["cash_addition"] or None
    return {
        "transaction_date": day.isoformat(),
        "currency_code": currency_code(entity_id),
        "draft": draft_payload(draft),
        "starting_cash_balance": out(starting_cash_balance(state)),
        "yesterday_closing_balance": out(state["yesterday_closing_balance"]),
        "safebox_balance": out(state["safebox_balance"]),
        "cash_addition": out(cash_addition) if cash_addition is not None else None,
        "withdrawal": state["withdrawal"],
    }


@router.get("/reports/opening")
def opening_page(request):
    require_edit(request)
    return opening_payload(request.company.id, query_day(request, required=False))


def _valid_save(body) -> dict:
    if not isinstance(body, dict):
        raise Refused("Send the opening as JSON.", 422)
    day = parse_day(body.get("transaction_date"))
    if day is None:
        raise Refused(BAD_DATE, 422)
    raw = body.get("cash_addition", 0)
    addition = amount(0 if raw is None else raw)
    if addition is None:
        raise Refused("Enter the cash added to the drawer as an amount of 0 or more.", 422)
    withdrawal = body.get("withdrawal")
    if withdrawal not in ("company", "personal", None):
        raise Refused("That withdrawal type doesn't look quite right to me.", 422)
    action = action_type(body)
    if action is None:
        raise Refused("Choose Save & Next or Save & Exit.", 422)
    return {"day": day, "cash_addition": addition, "withdrawal": withdrawal, "action_type": action}


@router.put("/reports/opening")
def save_opening_step(request):
    require_edit(request)
    entity_id = request.company.id
    save = _valid_save(json_body(request))
    day = save["day"]

    if not currency_code(entity_id):
        raise Refused(NO_CURRENCY, 409, to="dashboard")
    problem = new_report_date_problem(entity_id, day)
    if problem:
        raise Refused(problem, 409, to="dashboard")

    opening = starting_cash_balance(opening_state(entity_id, day))
    onboarding = first_report_opening(entity_id, day)
    if onboarding is not None:
        opening = onboarding
    if opening + save["cash_addition"] < 0:
        raise Refused(NEGATIVE, 409, to="opening")
    if submitted_report_for(entity_id, day):
        raise Refused(f"A report for {day} already exists. Please delete it first.", 409, to="dashboard")

    try:
        draft = save_opening(
            entity_id, request.auth_user, day, opening, save["cash_addition"], save["withdrawal"], save["action_type"]
        )
    except OpeningRefused as refused:
        raise Refused(refused.message, 409, to=refused.to) from None
    return progress_payload(draft, "sales" if save["action_type"] == "save_next" else "dashboard")
