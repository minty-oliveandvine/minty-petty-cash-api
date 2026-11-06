"""Why a failed report didn't publish to Xero, and whether each reason is fixed now.

Ported from Flask in Phase 3 of the Flask retirement (2026-11-05). It replaces the call to
Flask's ``POST /api/internal/xero/publish-reasons``. The Flask sources:
- ``report/services/history_query.py``: ``publish_reasons``, ``publish_reasons_for``;
- ``xero/services/publish_errors.py``: ``PublishFailureReason.reason_items_from_history``,
  ``latest_publish_reason_items``;
- ``xero/services/publish_resolution.py``: ``annotate_resolution``.

The publish writes each failure as a ``report_history`` row:
- ``action = "publish_failed"``;
- ``new_value`` = a JSON list of items ``{text, scope, expense_id, deps, ...}``.

Older rows hold plain strings, or the reason in the action itself (``publish_failed: ...``).

Each item is re-checked against the current state:
- **A contact** is asked of live Xero (``GET /Contacts/{id}``, once per contact per answer). If
  Xero cannot be asked, the synced ``xero_contact_sync`` table answers instead.
- **An account** is the database's word: archived accounts leave ``account_info`` on sync.

A check that fails never breaks the list. The reason then reads unresolved.
"""

from __future__ import annotations

import logging
import re

import requests
from django.conf import settings
from minty_db.models import (
    AccountInfo,
    Report,
    ReportExpense,
    ReportHistory,
    XeroContactSync,
)

from core.xero_tokens import XeroTokenUnavailable, xero_token
from petty_cash.services.xero_settings import account_for_role, contact_for_role
from petty_cash.xero.errors import PublishFailureReason

logger = logging.getLogger("petty-cash-api")

#: The most report ids one answer works out (the history page shows 10 a page).
MAX_REPORTS = 100
#: A live contact look-up is a badge, not a publish: it must not hold the page.
CONTACT_TIMEOUT = (5, 15)

_EXPENSE_TEXT_RE = re.compile(r"^Expense '(.+?)'")



# ---- the stored items ------------------------------------------------------------------------


def reason_items(action: str | None, new_value: str | None) -> list[dict]:
    """The items one ``report_history`` row holds (``reason_items_from_history``)."""
    return PublishFailureReason.reason_items_from_history(action, new_value)


def latest_reason_items(report: Report) -> list[dict]:
    """The items of the report's newest failed-publish row (``latest_publish_reason_items``)."""
    for row in ReportHistory.objects.filter(report_id=report.id).order_by("-created_at"):
        items = reason_items(row.action, row.new_value)
        if items:
            return items
    return []


# ---- is each reason fixed? -------------------------------------------------------------------


def _live_contact_active(entity_id, xero_contact_id: str) -> bool | None:
    """Xero's word on a contact: True when ACTIVE, False otherwise, None when Xero can't be asked."""
    try:
        token = xero_token(entity_id)
    except XeroTokenUnavailable:
        return None
    try:
        response = requests.get(
            f"{settings.XERO_API_BASE_URL}/Contacts/{xero_contact_id}",
            headers={"Authorization": f"Bearer {token.access_token}", "Accept": "application/json",
                     "Xero-Tenant-Id": str(token.xero_org_id)},
            timeout=CONTACT_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Publish reasons: Xero unreachable looking up contact %s: %s", xero_contact_id, exc)
        return None
    if response.status_code != 200:
        logger.warning("Publish reasons: Xero answered %s for contact %s", response.status_code, xero_contact_id)
        return None
    try:
        contacts = (response.json() or {}).get("Contacts") or []
    except ValueError:
        return None
    if contacts and isinstance(contacts[0], dict):
        return str(contacts[0].get("ContactStatus", "")).upper() == "ACTIVE"
    return False  # Xero did not return it: not active


def _contact_active(entity_id, xero_contact_id: str | None, cache: dict) -> bool:
    if not xero_contact_id:
        return False
    statuses = cache.setdefault("contact_status", {})
    if xero_contact_id not in statuses:
        statuses[xero_contact_id] = _live_contact_active(entity_id, xero_contact_id)
    live = statuses[xero_contact_id]
    if live is None:
        # Xero could not be asked: the (possibly stale) synced table answers.
        return XeroContactSync.objects.filter(entity_id=entity_id, xero_contact_id=xero_contact_id).exists()
    return live


def _account_not_known_bad(entity_id, account_code: str | None) -> bool:
    """True unless the code is a known account that is not ACTIVE (an unknown code is not held
    against the person)."""
    if not account_code:
        return True
    row = AccountInfo.objects.filter(entity_id=entity_id, xero_code=str(account_code)).first()
    return row is None or row.status == "ACTIVE"


def _role_account_valid(entity_id, role: str) -> bool:
    found = account_for_role(entity_id, role)
    if not found:
        return False
    return AccountInfo.objects.filter(id=found["account_id"], status="ACTIVE").exists()


def _role_contact_valid(entity_id, role: str, cache: dict) -> bool:
    try:
        contact = contact_for_role(entity_id, role)
    except ValueError:
        logger.warning("Publish reasons: unknown contact role %s", role)
        return False
    if contact is None or not contact.xero_contact_id:
        return False
    return _contact_active(entity_id, contact.xero_contact_id, cache)


def _dep_valid(entity_id, kind: str, role: str, cache: dict) -> bool:
    if kind == "contact":
        return _role_contact_valid(entity_id, role, cache)
    if kind == "account":
        return _role_account_valid(entity_id, role)
    return False


def _expense_targets(report_id, item: dict) -> list[ReportExpense] | None:
    """The expense lines an item is about; ``[]`` when they are gone; None when it is not about an
    expense. A text-only (legacy) row finds its line by name within the report."""
    expense_id = item.get("expense_id")
    if expense_id:
        return list(ReportExpense.objects.select_related("account", "contact").filter(id=expense_id))
    match = _EXPENSE_TEXT_RE.match(item.get("text") or "")
    if match and report_id:
        return list(ReportExpense.objects.select_related("account", "contact")
                    .filter(report_id=report_id, item=match.group(1)))
    return None


def _infer_deps(text: str | None) -> list[list[str]]:
    """What a legacy entity-wide reason with no stored deps depends on, from its wording."""
    t = (text or "").lower()
    if "cash sale" in t:
        return [["contact", "cashsale_contact"], ["account", "cash_sale"]]
    if "discrepancy" in t:
        return [["contact", "discrepancy_contact"], ["account", "discrepancy_account"]]
    if "deposit" in t:
        return [["account", "pettycash"], ["account", "bank"]]
    if "withdrawal" in t or "director" in t:
        return [["contact", "director_contact"], ["account", "director"], ["account", "pettycash"]]
    return []


def _resolve_item(entity_id, report_id, item: dict, cache: dict) -> tuple[bool, bool]:
    """``(resolved, resolvable)`` for one item."""
    targets = _expense_targets(report_id, item)
    if targets is not None:
        if not targets:
            return True, True  # the line was removed: nothing left to fix
        resolved = all(
            _contact_active(entity_id, line.contact.xero_contact_id if line.contact else None, cache)
            and _account_not_known_bad(entity_id, line.account.xero_code if line.account else None)
            for line in targets
        )
        return resolved, True
    deps = item.get("deps") or _infer_deps(item.get("text"))
    if deps:
        return all(_dep_valid(entity_id, kind, role, cache) for kind, role in deps), True
    return False, False


def annotate(entity_id, items: list[dict], report_id=None, cache: dict | None = None) -> list[dict]:
    """``[{text, resolved, resolvable}]`` for the items with text (``annotate_resolution``)."""
    cache = {} if cache is None else cache
    annotated = []
    for item in items or []:
        text = item.get("text")
        if not text:
            continue
        try:
            resolved, resolvable = _resolve_item(entity_id, report_id, item, cache)
        except Exception as exc:  # a badge never breaks the list; logged
            logger.warning("Publish reasons: could not re-check %r: %s", text, exc)
            resolved, resolvable = False, False
        annotated.append({"text": text, "resolved": resolved, "resolvable": resolvable})
    return annotated


# ---- the answer the routes use ---------------------------------------------------------------


def publish_reasons(entity_id, report_ids) -> dict[str, list[dict]]:
    """``{report id: [{text, resolved}]}`` for those of ``report_ids`` that are this company's; a
    report that did not fail has ``[]``. Live contact look-ups are shared across the reports."""
    reports = Report.objects.filter(entity_id=entity_id, id__in=[str(i) for i in report_ids])
    cache: dict = {}
    answer = {}
    for report in reports:
        if report.publishing_status != "failed":
            answer[str(report.id)] = []
            continue
        items = annotate(entity_id, latest_reason_items(report), report_id=report.id, cache=cache)
        answer[str(report.id)] = [{"text": r["text"], "resolved": bool(r["resolved"])} for r in items]
    return answer
