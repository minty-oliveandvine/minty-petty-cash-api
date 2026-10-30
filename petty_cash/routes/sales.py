"""The report wizard's Sales step - Flask's ``petty_cash_api/routes/sales.py``.

- ``GET /reports/sales?transaction_date=``: the day's draft (null before Opening made one), the
  company's electronic and delivery methods in Settings order with the day's amounts, and the cash.
- ``PUT /reports/sales`` ``{transaction_date, cash_sales, amounts: {value_name: amount}, action}``:
  a method left out is zero; an amount is 0 to 99,999,999,999.99; a method the company has not
  switched on is 422. No draft for the day is 409 ``{error, to: "opening"}``.
"""

from __future__ import annotations

from ninja import Router

from core.exceptions import Refused
from petty_cash.days import parse_day
from petty_cash.money import cents, out
from petty_cash.routes._input import (
    BAD_DATE,
    action_type,
    amount,
    json_body,
    query_day,
    require_edit,
)
from petty_cash.services.reports import currency_code, draft_payload, find_draft, progress_payload
from petty_cash.services.sales import CASH_VALUE_NAME, page_methods, save_sales

router = Router()
NO_DRAFT = "I don't see a draft yet - let's start with the opening entry."


def _cash(draft, methods) -> float | None:
    """What the page's cash field shows: the Cash method's row, else the report's cash column."""
    row = next((m["amount"] for m in methods if m["value_name"] == CASH_VALUE_NAME), None)
    if row:
        return out(row)
    return out(draft.cashsale_total) if draft is not None and draft.cashsale_total else None


def sales_payload(entity_id, day) -> dict:
    draft = find_draft(entity_id, day)
    methods = page_methods(entity_id, draft.id if draft is not None else None)
    return {
        "transaction_date": day.isoformat(),
        "currency_code": currency_code(entity_id),
        "draft": draft_payload(draft),
        "methods": [
            {**m, "amount": out(m["amount"]) if m["amount"] else None}
            for m in methods
            if m["value_name"] != CASH_VALUE_NAME
        ],
        "cash_sales": _cash(draft, methods),
    }


@router.get("/reports/sales")
def sales_page(request):
    require_edit(request)
    return sales_payload(request.company.id, query_day(request, required=True))


def _valid_save(body, value_names: dict) -> dict:
    if not isinstance(body, dict):
        raise Refused("Send the sales as JSON.", 422)
    day = parse_day(body.get("transaction_date"))
    if day is None:
        raise Refused(BAD_DATE, 422)
    raw_cash = body.get("cash_sales")
    cash = cents(0) if raw_cash is None else amount(raw_cash)
    if cash is None:
        raise Refused("Enter the cash sales as an amount of 0 or more.", 422)
    raw = body.get("amounts") or {}
    if not isinstance(raw, dict):
        raise Refused("Send the amounts as one per sales method.", 422)
    amounts = {}
    for value_name, value in raw.items():
        if value_name not in value_names:
            raise Refused(
                "One of those sales methods isn't switched on for this company. Reload the page to see its methods.", 422
            )
        typed = cents(0) if value is None else amount(value)
        if typed is None:
            raise Refused(f"Enter the {value_names[value_name]} sales as an amount of 0 or more.", 422)
        amounts[value_name] = typed
    action = action_type(body)
    if action is None:
        raise Refused("Choose Save & Next or Save & Exit.", 422)
    return {"day": day, "cash": cash, "amounts": amounts, "action_type": action}


@router.put("/reports/sales")
def save_sales_step(request):
    require_edit(request)
    entity_id = request.company.id
    value_names = {
        m["value_name"]: m["name"] for m in page_methods(entity_id) if m["value_name"] != CASH_VALUE_NAME
    }
    save = _valid_save(json_body(request), value_names)
    draft = find_draft(entity_id, save["day"])
    if draft is None:
        raise Refused(NO_DRAFT, 409, to="opening")
    draft = save_sales(entity_id, request.auth_user, draft, {**save["amounts"], CASH_VALUE_NAME: save["cash"]},
                       save["action_type"])
    return progress_payload(draft, "expenses" if save["action_type"] == "save_next" else "dashboard")
