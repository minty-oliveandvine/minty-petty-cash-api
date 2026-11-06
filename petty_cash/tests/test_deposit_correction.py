"""``POST /deposit-correction`` (``petty_cash/routes/home.py``, ``services/deposit_correction.py``):
the dashboard's deposit correction, done here since Phase 3 of the Flask retirement. Flask's
``tests/test_report_deposit_change.py`` and the correction tests of
``tests/test_petty_cash_api_home.py`` / ``test_petty_cash_internal_deposit_correction.py``,
restated against real rows. Xero is stubbed at ``requests`` and the token service at ``xero_token``.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
import requests
from minty_db.enums import EntityRole
from minty_db.models import (
    AccountInfo,
    CurrencyInfo,
    Entity,
    EntityPettycashSettings,
    Report,
    UserEntity,
    XeroReportSync,
)

from core.xero_tokens import XeroToken, XeroTokenUnavailable
from petty_cash.services import deposit_correction as dc
from petty_cash.tests.conftest import headers, make_report, make_user, url
from petty_cash.xero import publish_record

pytestmark = pytest.mark.django_db

DAY = date(2026, 10, 13)
BODY = {"kind": "change", "date": DAY.isoformat(), "amount": 3000}


class _Response:
    def __init__(self, status, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


class FakeXero:
    """``GET /BankTransfers`` answers ``transfers``; each ``POST /BankTransfers`` answers the next of
    ``posts`` (default: 200 with a new id)."""

    def __init__(self):
        self.transfers = [{"BankTransferID": "bt-orig", "Reference": "MT20261013Deposit", "Amount": 5000.0}]
        self.gets, self.posted = [], []
        self.posts: list = []
        self.token_problem: XeroTokenUnavailable | None = None

    def get(self, url, headers=None, params=None, timeout=None, **_):
        self.gets.append({"url": url, "params": params, "headers": headers})
        return _Response(200, {"BankTransfers": self.transfers})

    def post(self, url, data=None, headers=None, timeout=None, **_):
        assert timeout == (30, 60)
        payload = json.loads(data)["BankTransfers"][0]
        self.posted.append(payload)
        if self.posts:
            answer = self.posts.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return _Response(200, {"BankTransfers": [{"BankTransferID": f"bt-{len(self.posted)}"}]})

    def token(self, entity_id):
        if self.token_problem is not None:
            raise self.token_problem
        return XeroToken(access_token="tok", xero_org_id="org-1")


@pytest.fixture
def xero(monkeypatch) -> FakeXero:
    fake = FakeXero()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr(dc, "xero_token", fake.token)
    return fake


@pytest.fixture
def day(shop):
    """The previous day's submitted report (opening 20,000, deposit 5,000, closing 15,000), the
    next day's draft opened from it, and the deposit and petty cash accounts mapped."""
    shop.entity.xero_org_id = "org-1"
    shop.entity.save(update_fields=["xero_org_id"])
    bank = AccountInfo.objects.create(entity_id=shop.entity.id, xero_account_id="acc-bank", xero_code="090",
                                      name="Deposit Bank", type="BANK", status="ACTIVE")
    petty = AccountInfo.objects.create(entity_id=shop.entity.id, xero_account_id="acc-petty", xero_code="091",
                                       name="Petty Cash", type="BANK", status="ACTIVE")
    EntityPettycashSettings.objects.create(entity_id=shop.entity.id, bank_account_id=bank.id,
                                           pettycash_account_id=petty.id)
    report = make_report(shop.entity, DAY, created_by=shop.owner.id, opening_balance=20000, bank_deposit=5000,
                         closing_balance=15000)
    draft = make_report(shop.entity, DAY + timedelta(days=1), status="draft", created_by=shop.owner.id,
                        opening_balance=15000, adjusted_opening_balance=15000, closing_balance=15000)
    return report, draft


def _post(client, shop, body=BODY, user=None):
    return client.post(url(shop.entity.id, "/deposit-correction"), body, content_type="application/json",
                       headers=headers((user or shop.owner).id))


def _published(report):
    Report.objects.filter(id=report.id).update(publishing_status="completed", xero_integrated=True,
                                               status="published")


# ---- the report ------------------------------------------------------------------------------


def test_an_unpublished_change_corrects_the_day_and_the_next_draft_and_skips_xero(client, shop, day, xero):
    report, draft = day

    resp = _post(client, shop)

    assert resp.status_code == 200, resp.json()
    assert resp.json() == {
        "status": "success", "message": "Previous day's deposit updated successfully.", "id": str(report.id),
        "previous_deposit": 5000.0, "bank_deposit": 3000.0, "closing_balance": 17000.0, "is_published": False,
        "xero_status": "skipped", "next_transaction_date": "2026-10-14",
    }
    report.refresh_from_db()
    draft.refresh_from_db()
    assert (report.bank_deposit, report.closing_balance) == (Decimal("3000.00"), Decimal("17000.00"))
    assert (draft.opening_balance, draft.adjusted_opening_balance, draft.closing_balance) == (
        Decimal("17000.00"), Decimal("17000.00"), Decimal("17000.00"))
    assert xero.gets == [] and xero.posted == []


def test_no_deposit_zeroes_it(client, shop, day, xero):
    report, _ = day
    resp = _post(client, shop, {"kind": "no", "date": DAY.isoformat(), "amount": 0})
    assert resp.status_code == 200
    assert (resp.json()["bank_deposit"], resp.json()["closing_balance"]) == (0.0, 20000.0)


def test_the_closing_balance_keeps_the_counted_difference(client, shop, day, xero):
    report, _ = day
    Report.objects.filter(id=report.id).update(discrepancy_amount=Decimal("-50"))
    assert _post(client, shop).json()["closing_balance"] == 16950.0


def test_a_larger_deposit_is_refused_and_nothing_changes(client, shop, day, xero):
    report, _ = day
    resp = _post(client, shop, {**BODY, "amount": 5000.01})
    assert resp.status_code == 400 and resp.json() == {"error": dc.LARGER}
    report.refresh_from_db()
    assert report.bank_deposit == Decimal("5000.00")


@pytest.mark.parametrize("unmapped, named", [
    (("bank_account_id",), "Deposit Bank Account"),
    (("bank_account_id", "pettycash_account_id"), "Deposit Bank Account, Petty Cash Account"),
])
def test_an_unmapped_account_blocks_the_correction(client, shop, day, xero, unmapped, named):
    report, _ = day
    EntityPettycashSettings.objects.filter(entity_id=shop.entity.id).update(**{c: None for c in unmapped})

    resp = _post(client, shop)

    assert resp.status_code == 409
    assert f"not configured: {named}." in resp.json()["error"]
    report.refresh_from_db()
    assert report.bank_deposit == Decimal("5000.00") and xero.posted == []


def test_no_report_that_day_is_a_warning(client, shop, xero):
    resp = _post(client, shop)
    assert resp.status_code == 200
    assert resp.json() == {"status": "warning", "message": "No previous report found to update.",
                           "next_transaction_date": "2026-10-14"}


@pytest.mark.parametrize("body, error", [
    (["x"], "Send the correction as JSON."),
    ({**BODY, "kind": "maybe"}, "Choose whether the deposit changed or there was no deposit."),
    ({**BODY, "date": "13/10/2026"}, "The report date is missing. Reload the page and try again."),
    ({**BODY, "date": "2026-02-30"}, "The report date is missing. Reload the page and try again."),
    ({**BODY, "amount": -1}, "Enter the amount deposited, 0 or more."),
    ({**BODY, "amount": "12"}, "Enter the amount deposited, 0 or more."),
    ({**BODY, "amount": True}, "Enter the amount deposited, 0 or more."),
    ({"kind": "no", "date": DAY.isoformat(), "amount": 5}, "No deposit means an amount of 0."),
])
def test_a_bad_body_is_refused(client, shop, day, xero, body, error):
    resp = _post(client, shop, body)
    assert resp.status_code == 422 and resp.json() == {"error": error}


def test_a_cashier_may_not_correct_a_deposit(client, shop, day, xero):
    cashier = make_user("cashier@test.com")
    UserEntity.objects.create(user_id=cashier.id, entity_id=shop.entity.id, role=EntityRole.CASHIER, approved=True)
    resp = _post(client, shop, user=cashier)
    assert resp.status_code == 403
    assert Report.objects.get(id=day[0].id).bank_deposit == Decimal("5000.00")


# ---- Xero ------------------------------------------------------------------------------------


def test_a_published_change_reverses_and_replaces_the_deposit_in_xero(client, shop, day, xero):
    report, _ = day
    _published(report)
    publish_record.record_object(report.id, "org-1", "deposit", "bt-orig", object_type="BANK_TRANSFER", amount=5000.0)

    resp = _post(client, shop)

    assert resp.status_code == 200
    assert (resp.json()["status"], resp.json()["xero_status"], resp.json()["is_published"]) == ("success", "success", True)
    get, = xero.gets
    assert get["url"].endswith("/BankTransfers") and get["headers"]["Xero-Tenant-Id"] == "org-1"
    assert get["params"] == {"where": "Date>=DateTime(2026,10,13)&&Date<DateTime(2026,10,14)"}
    reversal, replacement = xero.posted
    assert reversal == {"FromBankAccount": {"AccountID": "acc-bank", "CurrencyCode": "HKD"},
                        "ToBankAccount": {"AccountID": "acc-petty", "CurrencyCode": "HKD"},
                        "Date": "2026-10-13", "Amount": 5000.0, "Reference": "MT20261013DepositReversal"}
    assert replacement == {"FromBankAccount": {"AccountID": "acc-petty", "CurrencyCode": "HKD"},
                           "ToBankAccount": {"AccountID": "acc-bank", "CurrencyCode": "HKD"},
                           "Date": "2026-10-13", "Amount": 3000.0, "Reference": "MT20261013DepositUpdated"}
    record = publish_record.load_record(report.id)
    assert record["objects"]["deposit"] == {"type": "BANK_TRANSFER", "id": "bt-2", "amount": 3000.0}
    assert record["org"] == "org-1"


def test_the_transfers_are_in_the_companys_currency_never_a_silent_hkd(client, shop, day, xero):
    report, _ = day
    _published(report)
    sgd = CurrencyInfo.objects.create(id=uuid.uuid4(), currency_code="SGD", currency_name="SGD", symbol="$",
                                      decimal_places=2)
    Entity.objects.filter(id=shop.entity.id).update(currency_id=sgd.id)

    assert _post(client, shop).json()["xero_status"] == "success"
    assert {p["FromBankAccount"]["CurrencyCode"] for p in xero.posted} == {"SGD"}
    assert {p["ToBankAccount"]["CurrencyCode"] for p in xero.posted} == {"SGD"}

    xero.posted.clear()
    xero.transfers = [{"BankTransferID": "bt-2", "Reference": "MT20261013DepositUpdated", "Amount": 3000.0}]
    Report.objects.filter(id=report.id).update(bank_deposit=3000, publishing_status="completed")
    Entity.objects.filter(id=shop.entity.id).update(currency_id=None)
    body = _post(client, shop, {**BODY, "amount": 1000}).json()
    assert body["message"] == ("Local report updated, but Xero sync failed: This company has no currency chosen, "
                               "so nothing was posted to Xero")
    assert xero.posted == []


def test_a_published_no_deposit_only_reverses_and_forgets_the_deposit(client, shop, day, xero):
    report, _ = day
    _published(report)
    publish_record.record_object(report.id, "org-1", "deposit", "bt-orig", object_type="BANK_TRANSFER")
    publish_record.record_object(report.id, "org-1", "invoices", "inv-1", object_type="INVOICE")

    resp = _post(client, shop, {"kind": "no", "date": DAY.isoformat(), "amount": 0})

    assert resp.json()["xero_status"] == "success"
    assert [p["Reference"] for p in xero.posted] == ["MT20261013DepositReversal"]
    assert set(publish_record.load_record(report.id)["objects"]) == {"invoices"}


def test_the_deposit_is_found_by_amount_when_its_reference_differs(client, shop, day, xero):
    report, _ = day
    _published(report)
    xero.transfers = [{"BankTransferID": "bt-x", "Reference": "Something else", "Amount": 3000},
                      {"BankTransferID": "bt-y", "Reference": "Hand typed", "Amount": "5000.00"}]
    assert _post(client, shop).json()["xero_status"] == "success"


def test_no_matching_transfer_is_a_warning_and_nothing_is_posted(client, shop, day, xero):
    report, _ = day
    _published(report)
    xero.transfers = [{"BankTransferID": "bt-x", "Reference": "MT20261013Deposit", "Amount": 4999}]

    body = _post(client, shop).json()

    assert (body["status"], body["xero_status"]) == ("warning", "failed")
    assert body["message"] == ("Local report updated, but Xero sync failed: No BankTransfer in Xero matched date "
                               "2026-10-13 and amount 5000 (saw 1 same-day transfer(s))")
    assert xero.posted == []
    assert Report.objects.get(id=report.id).bank_deposit == Decimal("3000.00")  # the report is corrected


def test_a_refused_reversal_is_a_warning_without_xeros_own_words(client, shop, day, xero):
    report, _ = day
    _published(report)
    xero.posts = [_Response(400, {"Message": "Cannot delete reconciled transfer"})]

    body = _post(client, shop).json()

    assert (body["status"], body["xero_status"]) == ("warning", "failed")
    assert "Could not post reversal BankTransfer in Xero" in body["message"]
    assert "reconciled" not in body["message"]  # Xero's text is logged, never shown
    assert len(xero.posted) == 1


def test_a_refused_replacement_is_a_warning_after_the_reversal(client, shop, day, xero):
    report, _ = day
    _published(report)
    xero.posts = [_Response(200, {"BankTransfers": [{"BankTransferID": "bt-rev"}]}), requests.ConnectionError("down")]

    body = _post(client, shop).json()

    assert body["xero_status"] == "failed"
    assert body["message"].startswith("Local report updated, but Xero sync failed: Reversal was posted in Xero")


@pytest.mark.parametrize("reconnect, sentence", [(True, "This entity is not connected to Xero"),
                                                 (False, "Xero could not be reached")])
def test_no_token_is_a_warning(client, shop, day, xero, reconnect, sentence):
    report, _ = day
    _published(report)
    xero.token_problem = XeroTokenUnavailable(reconnect=reconnect)

    body = _post(client, shop).json()

    assert body["message"] == f"Local report updated, but Xero sync failed: {sentence}"
    assert xero.gets == []


def test_an_unexpected_xero_failure_is_a_warning_never_the_exception(client, shop, day, xero, monkeypatch):
    report, _ = day
    _published(report)

    def boom(*args, **kwargs):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(dc, "_find_deposit_transfer", boom)
    body = _post(client, shop).json()

    assert body["message"] == "Local report updated, but Xero sync failed: Database updated but the Xero update failed."
    assert XeroReportSync.objects.filter(report_id=report.id).count() == 0


def test_a_published_report_with_no_previous_deposit_never_asks_xero(client, shop, day, xero):
    report, _ = day
    _published(report)
    Report.objects.filter(id=report.id).update(bank_deposit=0, closing_balance=20000)
    body = _post(client, shop, {"kind": "no", "date": DAY.isoformat(), "amount": 0}).json()
    assert body["xero_status"] == "skipped" and xero.gets == []
