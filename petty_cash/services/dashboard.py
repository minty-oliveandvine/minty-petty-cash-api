"""What the Petty Cash dashboard knows about a company - Flask's
``blueprints/entity/services/dashboard.py::dashboard_state``, read by ``GET /home``.

Kept as Flask has it, quirks included: the "latest report" includes drafts; the Continue card
reads the NEWEST draft while the calendar's floor is the EARLIEST draft's date; "published"
means anything not a draft. One fix Flask lacks (2026-10-26): only LIVE drafts count - a stale
draft, dated before the last submitted day, used to hide Start New Report for good
(``reports.live_drafts``). One deliberate difference: Flask's "today" for "today's report is
submitted" was the SERVER's local date (UTC on Render, so wrong for a Hong Kong morning) while
its date rules used Hong Kong's; here both are Hong Kong's (``petty_cash/days.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.db.models import Count, Min
from minty_db.enums import ReportStatus
from minty_db.models import CurrencyInfo, Entity, Report, User, UserEntity

from petty_cash.days import today
from petty_cash.services.reports import first_report_defaults, live_drafts
from petty_cash.services.xero_settings import account_for_role, missing_slots


def _first_names(user_ids) -> dict[str, str | None]:
    ids = {str(u) for u in user_ids if u}
    if not ids:
        return {}
    return {
        str(u.id): (u.first_name or "").strip() or None
        for u in User.objects.filter(id__in=ids).only("id", "first_name")
    }


def dashboard_state(entity_id: str) -> dict:
    entity_id = str(entity_id)
    reports = Report.objects.filter(entity_id=entity_id)

    latest_report = reports.order_by("-transaction_date").first()
    opening, start_date = first_report_defaults(entity_id)
    latest = {
        "date": None,
        # Before the first report the drawer holds onboarding's opening balance (0 without
        # one), not nothing (2026-10-27 copy test: a new company read 0.00 at HKD 5,525.00).
        "closing_balance": opening,
        "deposit": 0,
        "deposit_bank_name": "No deposit",
        "deposit_bank_number": "",
        "deposit_correction_missing_settings": [],
        "can_correct_deposit": True,
    }
    if latest_report is not None:
        latest["date"] = latest_report.transaction_date
        latest["closing_balance"] = latest_report.closing_balance
        latest["deposit"] = latest_report.bank_deposit
        if (latest_report.bank_deposit or 0) > 0:
            bank = account_for_role(entity_id, "bank")
            pettycash = account_for_role(entity_id, "pettycash")
            if bank:
                latest["deposit_bank_name"] = bank["name"]
                latest["deposit_bank_number"] = bank.get("bank_account_number", "****")
            else:
                latest["deposit_bank_name"] = "Unknown Bank"
                latest["deposit_bank_number"] = "****"
            latest["deposit_correction_missing_settings"] = [
                label
                for label, present in (("Deposit Bank Account", bool(bank)), ("Petty Cash Account", bool(pettycash)))
                if not present
            ]
            latest["can_correct_deposit"] = not latest["deposit_correction_missing_settings"]

    drafts = live_drafts(entity_id)
    current_draft = drafts.order_by("-transaction_date").first()
    earliest_draft_date = drafts.aggregate(first=Min("transaction_date"))["first"]
    draft = None
    if current_draft is not None:
        last_edit = current_draft.updated_at or current_draft.created_at
        if last_edit is not None and last_edit.tzinfo is None:
            last_edit = last_edit.replace(tzinfo=UTC)
        draft = {
            "id": current_draft.id,
            "current_section": current_draft.current_section,
            "completed_sections": current_draft.completed_sections or [],
            "last_edit_seconds": (
                int((datetime.now(UTC) - last_edit).total_seconds()) if last_edit is not None else 0
            ),
        }
    draft_date = earliest_draft_date or (current_draft.transaction_date if current_draft else None)

    published_rows = list(
        reports.exclude(status=ReportStatus.DRAFT).values(
            "transaction_date", "id", "closing_balance", "bank_deposit", "xero_integrated", "created_by"
        )
    )
    names = _first_names(
        [r["created_by"] for r in published_rows] + ([latest_report.created_by] if latest_report else [])
    )
    published = {
        r["transaction_date"].isoformat(): {
            "id": r["id"],
            "closing_balance": r["closing_balance"],
            "bank_deposit": r["bank_deposit"] or 0,
            "xero_integrated": r["xero_integrated"],
            "submitter_first_name": names.get(str(r["created_by"])) if r["created_by"] else None,
        }
        for r in published_rows
    }

    today_iso = today().isoformat()
    latest_iso = latest_report.transaction_date.isoformat() if latest_report else None
    missing = missing_slots(entity_id)

    org = Entity.objects.filter(id=entity_id).only("currency_id").first()
    currency_code = None
    if org is not None and org.currency_id:
        code = CurrencyInfo.objects.filter(id=org.currency_id).values_list("currency_code", flat=True).first()
        currency_code = code.strip() if code else None

    return {
        "latest": latest,
        "latest_submitter_first_name": (
            names.get(str(latest_report.created_by)) if latest_report and latest_report.created_by else None
        ),
        "draft": draft,
        "draft_date": draft_date,
        # Onboarding's start date: the first report's floor (onboarding seeds no draft any more).
        "start_date": start_date,
        "published": published,
        "today_has_report": today_iso in published,
        "today_is_latest_report": today_iso == latest_iso,
        "has_complete_xero_settings": not missing,
        "missing_settings_fields": missing,
        "server_today_hk": today_iso,
        "currency_code": currency_code,
    }


def member_count(entity_id: str) -> int:
    """The company's approved members (the dashboard's "N users")."""
    return UserEntity.objects.filter(entity_id=entity_id, approved=True).aggregate(n=Count("user_id"))["n"] or 0
