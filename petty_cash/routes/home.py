"""The Petty Cash dashboard - Flask's ``petty_cash_api/routes/home.py``.

- ``GET /home``: everything the page computes (``petty_cash/services/dashboard.py``) plus what this
  person may do there.
- ``POST /deposit-correction`` ``{kind, date, amount}``: the "Before You Start" correction of the
  previous day's bank deposit. It can reverse and re-post the deposit in Xero, so Flask does it
  (``core/accounts_api.py``, the owner's choice of 2026-10-15): this route asks
  ``REPORT_EDIT_ENTITY`` itself, then hands the body to Flask's internal route and Flask's answer -
  the Petty Cash API's own JSON, refusals included - back unchanged. Flask checks the person and the
  body again.
"""

from __future__ import annotations

from django.http import JsonResponse
from ninja import Router

from core.accounts_api import forward_deposit_correction
from core.exceptions import Refused
from core.policy import Permission, has_permission, is_superuser_readonly
from petty_cash.days import iso
from petty_cash.money import out
from petty_cash.routes._input import json_body
from petty_cash.services.dashboard import dashboard_state, member_count

router = Router()

NOT_ALLOWED = "You don't have permission to change this company's reports."


def home_payload(entity_id: str, person) -> dict:
    state = dashboard_state(entity_id)
    latest, draft = state["latest"], state["draft"]
    return {
        "server_today": state["server_today_hk"],
        # Quick Actions' "N users": the company's approved members (Flask's page always said 1).
        "user_count": member_count(entity_id),
        "currency_code": state["currency_code"],
        "latest": {
            "date": iso(latest["date"]),
            "closing_balance": out(latest["closing_balance"]),
            "deposit": out(latest["deposit"]),
            "deposit_bank_name": latest["deposit_bank_name"],
            "deposit_bank_number": latest["deposit_bank_number"],
            "deposit_correction_missing_settings": latest["deposit_correction_missing_settings"],
            "can_correct_deposit": latest["can_correct_deposit"],
            "submitter_first_name": state["latest_submitter_first_name"],
        },
        "draft": (
            {
                "id": str(draft["id"]),
                "current_section": draft["current_section"],
                "completed_sections": list(draft["completed_sections"]),
                "last_edit_seconds": draft["last_edit_seconds"],
            }
            if draft
            else None
        ),
        "draft_date": iso(state["draft_date"]),
        "start_date": iso(state["start_date"]),
        "published": {
            day: {
                "id": str(row["id"]),
                "closing_balance": out(row["closing_balance"]),
                "bank_deposit": out(row["bank_deposit"]),
                "xero_integrated": bool(row["xero_integrated"]),
                "submitter_first_name": row["submitter_first_name"],
            }
            for day, row in state["published"].items()
        },
        "today_has_report": state["today_has_report"],
        "today_is_latest_report": state["today_is_latest_report"],
        "xero_settings": {
            "complete": state["has_complete_xero_settings"],
            "missing": list(state["missing_settings_fields"]),
        },
        "permissions": {
            "read_only": is_superuser_readonly(person, entity_id),
            "correct_deposit": has_permission(person, Permission.REPORT_EDIT_ENTITY, entity_id),
        },
    }


@router.get("/home")
def home(request):
    return home_payload(request.company.id, request.auth_user)


@router.post("/deposit-correction")
def deposit_correction(request):
    entity_id = request.company.id
    if not has_permission(request.auth_user, Permission.REPORT_EDIT_ENTITY, entity_id):
        raise Refused(NOT_ALLOWED, 403)
    answer, status = forward_deposit_correction(entity_id, request.auth_user.id, json_body(request))
    return JsonResponse(answer, status=status)
