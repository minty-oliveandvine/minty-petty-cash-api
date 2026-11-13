"""Why a failed report didn't publish, and whether each reason is fixed now
(``petty_cash/services/publish_reasons.py``, ported from Flask in Phase 3 of the Flask retirement).

Flask's own tests stubbed this resolution out; these pin its rules. The stored rows are real
``report_history`` rows. Xero is stubbed at ``requests.get`` and the token service at
``xero_token``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import requests
from minty_db.models import (
    AccountInfo,
    EntityPettycashSettings,
    ReportExpense,
    ReportHistory,
    XeroContactSync,
)

from core.xero_tokens import XeroToken, XeroTokenUnavailable
from petty_cash.services import publish_reasons as pr
from petty_cash.tests.conftest import make_entity, make_report, make_user
from petty_cash.tests.test_close import D3

pytestmark = pytest.mark.django_db


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeXero:
    """``GET /Contacts/{id}`` answers ``statuses[id]`` (ACTIVE / ARCHIVED); ``down`` raises."""

    def __init__(self):
        self.statuses: dict[str, str] = {}
        self.asked: list[str] = []
        self.down = False

    def get(self, url, headers=None, timeout=None, **_):
        contact_id = url.rsplit("/", 1)[-1]
        self.asked.append(contact_id)
        assert headers["Xero-Tenant-Id"] == "org-1" and headers["Authorization"] == "Bearer tok"
        assert timeout == pr.CONTACT_TIMEOUT
        if self.down:
            raise requests.ConnectionError("down")
        status = self.statuses.get(contact_id)
        return _Resp(200, {"Contacts": [{"ContactID": contact_id, "ContactStatus": status}] if status else []})


@pytest.fixture
def xero(monkeypatch) -> FakeXero:
    fake = FakeXero()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(pr, "xero_token", lambda entity_id: XeroToken(access_token="tok", xero_org_id="org-1"))
    return fake


def _failed(shop, items, *, days_back=0, action="publish_failed", at=None):
    report = make_report(shop.entity, D3 - timedelta(days=days_back), created_by=shop.owner.id,
                         publishing_status="failed")
    _history(report, items, action=action, at=at)
    return report


def _history(report, items, *, action="publish_failed", at=None):
    row = ReportHistory.objects.create(id=uuid.uuid4(), report_id=report.id, action=action,
                                       new_value=items if isinstance(items, str) or items is None else json.dumps(items))
    if at is not None:
        ReportHistory.objects.filter(id=row.id).update(created_at=at)
    return row


def _contact(shop, xero_contact_id, name="ABC Supplies"):
    return XeroContactSync.objects.create(entity_id=shop.entity.id, xero_contact_id=xero_contact_id, name=name)


def _account(shop, code, status="ACTIVE"):
    return AccountInfo.objects.create(entity_id=shop.entity.id, xero_account_id=f"acc-{code}", xero_code=code,
                                      name=f"Account {code}", type="EXPENSE", status=status)


def _line(report, item, *, contact=None, account=None):
    return ReportExpense.objects.create(report_id=report.id, item=item, amount=5,
                                        contact_id=contact.id if contact else None,
                                        account_id=account.id if account else None)


def _reasons(shop, report):
    return pr.publish_reasons(shop.entity.id, [report.id])[str(report.id)]


# ---- an expense line's reason ----------------------------------------------------------------


def test_an_expense_with_an_active_contact_and_account_is_resolved(shop, xero):
    contact, account = _contact(shop, "con-1"), _account(shop, "400")
    xero.statuses["con-1"] = "ACTIVE"
    report = make_report(shop.entity, D3, created_by=shop.owner.id, publishing_status="failed")
    line = _line(report, "Coffee", contact=contact, account=account)
    _history(report, [{"text": "Expense 'Coffee' - contact is archived in Xero", "scope": "expense",
                       "expense_id": str(line.id)}])

    assert _reasons(shop, report) == [{"text": "Expense 'Coffee' - contact is archived in Xero", "resolved": True}]


def test_an_archived_contact_in_live_xero_is_not_resolved_even_if_synced(shop, xero):
    contact = _contact(shop, "con-1")
    xero.statuses["con-1"] = "ARCHIVED"
    report = make_report(shop.entity, D3, created_by=shop.owner.id, publishing_status="failed")
    line = _line(report, "Coffee", contact=contact)
    _history(report, [{"text": "Expense 'Coffee' - archived", "expense_id": str(line.id)}])

    assert _reasons(shop, report)[0]["resolved"] is False


def test_xero_unreachable_falls_back_to_the_synced_table(shop, xero):
    xero.down = True
    synced = _contact(shop, "con-1")
    report = make_report(shop.entity, D3, created_by=shop.owner.id, publishing_status="failed")
    line = _line(report, "Coffee", contact=synced)
    _history(report, [{"text": "Expense 'Coffee' - x", "expense_id": str(line.id)}])
    assert _reasons(shop, report)[0]["resolved"] is True  # synced for this company -> active

    XeroContactSync.objects.filter(id=synced.id).update(entity_id=None)  # no longer this company's
    assert _reasons(shop, report)[0]["resolved"] is False


def test_no_token_counts_as_xero_unreachable(shop, xero, monkeypatch):
    def refused(entity_id):
        raise XeroTokenUnavailable(reconnect=True)

    monkeypatch.setattr(pr, "xero_token", refused)
    contact = _contact(shop, "con-1")
    report = make_report(shop.entity, D3, created_by=shop.owner.id, publishing_status="failed")
    line = _line(report, "Coffee", contact=contact)
    _history(report, [{"text": "Expense 'Coffee' - x", "expense_id": str(line.id)}])

    assert _reasons(shop, report)[0]["resolved"] is True
    assert xero.asked == []


def test_a_known_inactive_account_holds_the_reason_open_and_an_unknown_code_does_not(shop, xero):
    contact = _contact(shop, "con-1")
    xero.statuses["con-1"] = "ACTIVE"
    archived = _account(shop, "410", status="ARCHIVED")
    report = make_report(shop.entity, D3, created_by=shop.owner.id, publishing_status="failed")
    line = _line(report, "Coffee", contact=contact, account=archived)
    _history(report, [{"text": "Expense 'Coffee' - x", "expense_id": str(line.id)}])
    assert _reasons(shop, report)[0]["resolved"] is False

    ReportExpense.objects.filter(id=line.id).update(account_id=None)
    assert _reasons(shop, report)[0]["resolved"] is True


def test_a_removed_expense_line_is_resolved(shop, xero):
    report = _failed(shop, [{"text": "Expense 'Coffee' - x", "scope": "expense", "expense_id": str(uuid.uuid4())}])
    assert _reasons(shop, report) == [{"text": "Expense 'Coffee' - x", "resolved": True}]


def test_a_legacy_text_only_row_finds_its_line_by_name(shop, xero):
    contact = _contact(shop, "con-1")
    xero.statuses["con-1"] = "ARCHIVED"
    report = make_report(shop.entity, D3, created_by=shop.owner.id, publishing_status="failed")
    _line(report, "Coffee", contact=contact)
    _history(report, ["Expense 'Coffee' - contact is archived in Xero"])  # old rows: plain strings

    assert _reasons(shop, report)[0]["resolved"] is False
    xero.statuses["con-1"] = "ACTIVE"
    assert _reasons(shop, report)[0]["resolved"] is True


def test_one_live_look_up_per_contact_across_the_reports(shop, xero):
    contact = _contact(shop, "con-1")
    xero.statuses["con-1"] = "ACTIVE"
    ids = []
    for days_back in (0, 1):
        report = make_report(shop.entity, D3 - timedelta(days=days_back), created_by=shop.owner.id,
                             publishing_status="failed")
        line = _line(report, "Coffee", contact=contact)
        _history(report, [{"text": "Expense 'Coffee' - x", "expense_id": str(line.id)}])
        ids.append(report.id)

    pr.publish_reasons(shop.entity.id, ids)
    assert xero.asked == ["con-1"]


# ---- a company-wide reason -------------------------------------------------------------------


def test_stored_deps_are_re_checked(shop, xero):
    report = _failed(shop, [{"text": "Cash sales - no cash sale contact", "scope": "entity",
                             "deps": [["contact", "cashsale_contact"], ["account", "cash_sale"]]}])
    assert _reasons(shop, report)[0]["resolved"] is False  # nothing mapped

    contact, account = _contact(shop, "con-9", "Cash Sale"), _account(shop, "200")
    EntityPettycashSettings.objects.create(entity_id=shop.entity.id, cash_sale_contact_id=contact.id,
                                           cash_sale_account_id=account.id)
    xero.statuses["con-9"] = "ACTIVE"
    assert _reasons(shop, report)[0]["resolved"] is True

    AccountInfo.objects.filter(id=account.id).update(status="ARCHIVED")
    assert _reasons(shop, report)[0]["resolved"] is False


def test_a_legacy_company_reason_infers_its_deps_from_the_wording(shop, xero):
    bank, petty = _account(shop, "090"), _account(shop, "091")
    EntityPettycashSettings.objects.create(entity_id=shop.entity.id, bank_account_id=bank.id,
                                           pettycash_account_id=petty.id)
    report = _failed(shop, None, action="publish_failed: Deposit - bank account missing")

    assert _reasons(shop, report) == [{"text": "Deposit - bank account missing", "resolved": True}]


def _discrepancy_mapped(shop, xero, *, petty_status="ACTIVE"):
    """A company whose discrepancy contact and account are fine; only petty cash's status varies."""
    contact = _contact(shop, "con-d", "Discrepancy")
    xero.statuses["con-d"] = "ACTIVE"
    account, petty = _account(shop, "480"), _account(shop, "1011", status=petty_status)
    EntityPettycashSettings.objects.create(entity_id=shop.entity.id, discrepancy_contact_id=contact.id,
                                           discrepancy_account_id=account.id, pettycash_account_id=petty.id,
                                           discrepancy_bank_account_id=petty.id)
    return petty


OLD_DISCREPANCY_DEPS = [["contact", "discrepancy_contact"], ["account", "discrepancy_account"]]


@pytest.mark.parametrize("deps", [OLD_DISCREPANCY_DEPS, None], ids=["stored-before-the-fix", "legacy-wording"])
def test_a_discrepancy_stays_open_while_the_petty_cash_account_it_posts_into_is_archived(shop, xero, deps):
    """The discrepancy posts into the petty cash account; rows stored before its deps named it
    are still held open by it (2026-11-13: a ticked Discrepancy that kept failing)."""
    petty = _discrepancy_mapped(shop, xero, petty_status="ARCHIVED")
    item = {"text": "Discrepancy - account is no longer active in Xero", "scope": "entity",
            "module": "discrepancy"}
    if deps is not None:
        item["deps"] = deps
    report = _failed(shop, [item])
    assert _reasons(shop, report)[0]["resolved"] is False

    AccountInfo.objects.filter(id=petty.id).update(status="ACTIVE")
    assert _reasons(shop, report)[0]["resolved"] is True


def test_the_discrepancy_step_names_the_petty_cash_account_it_posts_into():
    from petty_cash.xero import steps

    assert ["account", "pettycash"] in steps.DEPS_DISCREPANCY


def test_an_expense_stays_open_while_the_mapped_petty_cash_account_is_archived(shop, xero):
    contact, account = _contact(shop, "con-1"), _account(shop, "400")
    xero.statuses["con-1"] = "ACTIVE"
    petty = _account(shop, "1011", status="ARCHIVED")
    EntityPettycashSettings.objects.create(entity_id=shop.entity.id, pettycash_account_id=petty.id)
    report = make_report(shop.entity, D3, created_by=shop.owner.id, publishing_status="failed")
    line = _line(report, "Coffee", contact=contact, account=account)
    _history(report, [{"text": "Expense 'Coffee' - x", "scope": "expense", "expense_id": str(line.id)}])
    assert _reasons(shop, report)[0]["resolved"] is False

    AccountInfo.objects.filter(id=petty.id).update(status="ACTIVE")
    assert _reasons(shop, report)[0]["resolved"] is True


def test_a_reason_with_nothing_to_re_check_stays_open(shop, xero):
    report = _failed(shop, [{"text": "Xero connection has expired"}])
    assert _reasons(shop, report) == [{"text": "Xero connection has expired", "resolved": False}]


# ---- which rows, which reports ---------------------------------------------------------------


def test_only_the_newest_failed_row_counts(shop, xero):
    now = datetime.now(UTC)
    report = _failed(shop, [{"text": "Old reason"}], at=now - timedelta(hours=2))
    _history(report, None, action="edited", at=now - timedelta(hours=1))  # not a publish failure
    _history(report, [{"text": "New reason"}], at=now)

    assert [r["text"] for r in _reasons(shop, report)] == ["New reason"]


def test_a_report_that_did_not_fail_has_no_reasons(shop, xero):
    report = make_report(shop.entity, D3, created_by=shop.owner.id, publishing_status="completed")
    _history(report, [{"text": "Old failure"}])
    assert _reasons(shop, report) == []


def test_another_companys_report_is_left_out(shop, xero):
    stranger = make_user("stranger@test.com")
    theirs = make_entity(stranger, name="Their Shop")
    report = _failed(shop, [{"text": "Something"}])

    assert pr.publish_reasons(theirs.id, [report.id]) == {}


def test_items_without_text_are_skipped_and_bad_json_is_one_reason(shop, xero):
    report = _failed(shop, [{"text": ""}, {"scope": "entity"}, "  "])
    assert _reasons(shop, report) == []
    _history(report, "Plain words, not JSON")
    assert _reasons(shop, report) == [{"text": "Plain words, not JSON", "resolved": False}]
