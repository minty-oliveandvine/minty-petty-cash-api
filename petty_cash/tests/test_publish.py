"""Publishing to Xero (``petty_cash/routes/report.py``, ``services/publish.py``,
``petty_cash/xero/steps.py``), done here since Phase 3 of the Flask retirement.

Restated from Flask's tests:
- ``tests/test_report_publish_state.py`` and the publishing section of
  ``test_petty_cash_api_report.py``: the start, its refusals and the lock;
- ``tests/test_xero_report_republish.py`` and ``test_xero_expense_receipts.py``: the steps, a
  republish updating in place, the sweep and the receipts.

Xero is faked at ``requests`` (every call recorded) and the token service at ``xero_token``. The
worker runs in the test's own thread (``run_publish``); the start's thread is only recorded.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
import requests
from minty_db.enums import EntityRole
from minty_db.models import (
    AccountInfo,
    Attachment,
    CurrencyInfo,
    Entity,
    EntityPettycashSettings,
    Report,
    ReportExpense,
    ReportExpenseAttachment,
    ReportHistory,
    UserEntity,
    XeroContactSync,
)

from core.xero_tokens import XeroToken, XeroTokenUnavailable
from petty_cash.services import publish as pub
from petty_cash.tests.conftest import headers, make_entity, make_report, make_user, url
from petty_cash.tests.test_close import D3
from petty_cash.xero import publish_record, receipts, steps

pytestmark = pytest.mark.django_db
ORG = "org-1"
REASON = {"text": "No account code for Fees", "resolved": True}


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
    """Xero's accounting and files APIs. ``answers[(method, path prefix)]`` overrides an answer
    (a ``_Response`` or an exception); every call lands in ``calls`` as ``(method, path, body)``."""

    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self.answers: dict[tuple[str, str], object] = {}
        self.attached: dict[str, list[dict]] = {}  # transaction id -> its files
        self.n = 0

    def _path(self, url):
        for base in ("https://api.xero.com/api.xro/2.0/", "https://api.xero.com/files.xro/1.0/",
                     "https://api.xero.com/"):
            if url.startswith(base):
                return url[len(base):]
        raise AssertionError(f"unexpected URL {url}")

    def _override(self, method, path):
        for (m, prefix), answer in self.answers.items():
            if m == method and path.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return None

    def _id(self, prefix):
        self.n += 1
        return f"{prefix}-{self.n}"

    def request(self, method, url, body):
        path = self._path(url)
        self.calls.append((method, path, body))
        answer = self._override(method, path)
        if answer is not None:
            return answer
        if method == "GET" and path == "connections":
            return _Response(200, [{"tenantId": ORG}])
        if method == "GET" and path.startswith("Associations/"):
            files = self.attached.get(path.split("/", 1)[1])
            return _Response(200, files) if files else _Response(404, [])
        if method == "GET" and path.startswith("Contacts/"):
            return _Response(200, {"Contacts": [{"ContactID": path.split("/", 1)[1], "ContactStatus": "ACTIVE"}]})
        if path.startswith("BankTransactions"):
            return _Response(200, {"BankTransactions": [{"BankTransactionID": path.split("/", 1)[1] if "/" in path
                                                         else self._id("btx")}]})
        if path.startswith("BankTransfers/"):
            return _Response(200, {})
        if path == "BankTransfers":
            return _Response(200, {"BankTransfers": [{"BankTransferID": self._id("btr")}]})
        if path.startswith("Invoices"):
            return _Response(200, {"Invoices": [{"InvoiceID": path.split("/", 1)[1] if "/" in path
                                                 else self._id("inv")}]})
        if path == "Contacts":
            return _Response(200, {"Contacts": [{"ContactID": self._id("con"), "Name": body["Contacts"][0]["Name"]}]})
        if path == "Files":
            return _Response(201, {"FileId": self._id("file")})
        if path.endswith("/Associations"):
            return _Response(200, {})
        if method == "DELETE":
            return _Response(204)
        raise AssertionError(f"unhandled {method} {path}")

    def get(self, url, headers=None, params=None, timeout=None, **_):
        return self.request("GET", url, params)

    def post(self, url, data=None, json=None, files=None, headers=None, timeout=None, **_):
        body = json if json is not None else (files and list(files)) or (data and __import__("json").loads(data))
        return self.request("POST", url, body)

    def put(self, url, data=None, headers=None, timeout=None, **_):
        return self.request("PUT", url, __import__("json").loads(data))

    def delete(self, url, headers=None, timeout=None, **_):
        return self.request("DELETE", url, None)

    def sent(self, method, prefix):
        return [b for m, p, b in self.calls if m == method and p.startswith(prefix)]


@pytest.fixture
def xero(monkeypatch) -> FakeXero:
    fake = FakeXero()
    for verb in ("get", "post", "put", "delete"):
        monkeypatch.setattr(requests, verb, getattr(fake, verb))
    monkeypatch.setattr(pub, "xero_token", lambda entity_id: XeroToken(access_token="tok", xero_org_id=ORG))
    monkeypatch.setattr(receipts.storage, "read_receipt", lambda key, limit: b"receipt-bytes")
    monkeypatch.setattr(steps, "BATCH_PAUSE", 0)
    return fake


@pytest.fixture
def threads(monkeypatch) -> list:
    started = []

    class Recorder:
        def __init__(self, target=None, args=(), daemon=None):
            started.append(args)

        def start(self):
            pass

    monkeypatch.setattr(pub.threading, "Thread", Recorder)
    return started


@pytest.fixture
def mapped(shop):
    """A company connected to Xero with all nine Petty Cash slots mapped."""
    Entity.objects.filter(id=shop.entity.id).update(xero_org_id=ORG, status="connected")
    shop.entity.refresh_from_db()

    def account(code, name, kind="BANK"):
        return AccountInfo.objects.create(entity_id=shop.entity.id, xero_account_id=f"acc-{code}", xero_code=code,
                                          name=name, type=kind, status="ACTIVE")

    def contact(xid, name):
        return XeroContactSync.objects.create(entity_id=shop.entity.id, xero_contact_id=xid, name=name)

    EntityPettycashSettings.objects.create(
        entity_id=shop.entity.id,
        pettycash_account_id=account("091", "Petty Cash").id, bank_account_id=account("090", "Deposit Bank").id,
        cash_sale_account_id=account("200", "Sales", "REVENUE").id,
        director_account_id=account("880", "Director", "EQUITY").id,
        discrepancy_account_id=account("420", "Cash Over Short", "EXPENSE").id,
        discrepancy_bank_account_id=account("092", "Discrepancy Bank").id,
        cash_sale_contact_id=contact("con-cash", "Cash Sale").id,
        director_contact_id=contact("con-dir", "Olive Director").id,
        discrepancy_contact_id=contact("con-disc", "Discrepancy").id,
    )
    return shop


def _report(shop, **fields) -> Report:
    values = {"opening_balance": 100, "closing_balance": 0, "completed_sections": ["submitted"]}
    values.update(fields)
    return make_report(shop.entity, D3, created_by=shop.owner.id, **values)


def _line(shop, report, item, amount, *, code="429", contact="con-sup", receipt=None):
    account = (AccountInfo.objects.filter(entity_id=shop.entity.id, xero_code=code).first()
               or AccountInfo.objects.create(entity_id=shop.entity.id, xero_account_id=f"acc-{code}", xero_code=code,
                                             name=f"Account {code}", type="EXPENSE", status="ACTIVE"))
    supplier = (XeroContactSync.objects.filter(entity_id=shop.entity.id, xero_contact_id=contact).first()
                or XeroContactSync.objects.create(entity_id=shop.entity.id, xero_contact_id=contact, name="ABC Supplies"))
    line = ReportExpense.objects.create(report_id=report.id, item=item, amount=amount, account_id=account.id,
                                        contact_id=supplier.id, remarks=f"{item} receipt")
    if receipt:
        attachment = Attachment.objects.create(id=uuid.uuid4(), original_name="r.png", stored_name="r.png",
                                               file_path=receipt, file_extension="png")
        ReportExpenseAttachment.objects.create(report_expense_id=line.id, attachment_id=attachment.id, sort_order=0)
    return line


def _full_day(shop):
    """A day with every step: a company withdrawal, cash sales, two receipted expenses, a deposit
    and a short count."""
    report = _report(shop, cash_addition=50, cash_addition_type="company", cashsale_total=200, expense_total=30,
                     bank_deposit=150, discrepancy_amount=-5, discrepancy_type="short",
                     discrepancy_reason="Short by 5")
    first = _line(shop, report, "Coffee", 10, receipt="r/coffee.png")
    second = _line(shop, report, "Taxi", 20, receipt="r/taxi.png")
    return report, first, second


def _publish(client, shop, report_id, user=None):
    return client.post(url(shop.entity.id, f"/reports/{report_id}/publish"), headers=headers((user or shop.owner).id))


def _status(client, shop, report_id):
    return client.get(url(shop.entity.id, f"/reports/{report_id}/publishing-status"), headers=headers(shop.owner.id))


def _run(shop, report, prior=None):
    pub.run_publish(shop.entity.id, report.id, shop.owner.id, prior)
    report.refresh_from_db()
    return report


# ---- the start -------------------------------------------------------------------------------


def test_the_start_locks_the_report_and_starts_the_worker(client, mapped, xero, threads):
    report, *_ = _full_day(mapped)

    resp = _publish(client, mapped, report.id)

    assert resp.status_code == 202
    assert resp.json() == {"publishing_status": "publishing", "message": "Publishing to Xero. This can take a few minutes."}
    assert Report.objects.get(id=report.id).publishing_status == "publishing"
    assert [tuple(str(a) for a in args) for args in threads] == [
        (str(mapped.entity.id), str(report.id), str(mapped.owner.id), "unpublished")]


def test_a_failed_publish_starts_again_knowing_it_failed(client, mapped, xero, threads):
    report, *_ = _full_day(mapped)
    Report.objects.filter(id=report.id).update(publishing_status="failed")
    assert _publish(client, mapped, report.id).status_code == 202
    assert threads[0][3] == "failed"


def test_a_report_already_publishing_is_refused(client, mapped, xero, threads):
    report, *_ = _full_day(mapped)
    Report.objects.filter(id=report.id).update(publishing_status="publishing")

    resp = _publish(client, mapped, report.id)

    assert resp.status_code == 409 and resp.json() == {"error": pub.ALREADY}
    assert threads == []


@pytest.mark.parametrize("reconnect, status, body", [
    (True, 400, {"error": pub.RECONNECT, "reconnect": True}),
    (False, 503, {"error": pub.NO_TOKEN_SERVICE}),
])
def test_no_token_is_refused_before_anything(client, mapped, xero, threads, monkeypatch, reconnect, status, body):
    def refused(entity_id):
        raise XeroTokenUnavailable(reconnect=reconnect)

    monkeypatch.setattr(pub, "xero_token", refused)
    report, *_ = _full_day(mapped)

    resp = _publish(client, mapped, report.id)

    assert (resp.status_code, resp.json()) == (status, body)
    assert Report.objects.get(id=report.id).publishing_status != "publishing" and threads == []


def test_incomplete_xero_settings_are_refused(client, shop, xero, threads):
    Entity.objects.filter(id=shop.entity.id).update(xero_org_id=ORG, status="connected")
    report = _report(shop)
    resp = _publish(client, shop, report.id)
    assert (resp.status_code, resp.json()) == (400, {"error": pub.INCOMPLETE})


def test_a_draft_is_refused(client, mapped, xero, threads):
    report = _report(mapped, status="draft")
    resp = _publish(client, mapped, report.id)
    assert (resp.status_code, resp.json()) == (409, {"error": pub.DRAFT})


def test_a_system_account_line_is_refused(client, mapped, xero, threads):
    report = _report(mapped, expense_total=10)
    _line(mapped, report, "Rounding", 10, code="497")
    resp = _publish(client, mapped, report.id)
    assert (resp.status_code, resp.json()) == (400, {"error": "System accounts are not allowed to be published to Xero."})


def test_a_company_not_marked_connected_is_re_synced_from_xero(client, mapped, xero, threads):
    Entity.objects.filter(id=mapped.entity.id).update(status="disconnected")
    report, *_ = _full_day(mapped)

    assert _publish(client, mapped, report.id).status_code == 202
    assert Entity.objects.get(id=mapped.entity.id).status == "connected"
    assert ("GET", "connections", None) in xero.calls


def test_an_unknown_or_another_companys_report_is_404(client, mapped, xero, threads):
    stranger = make_user("stranger@test.com")
    theirs = make_report(make_entity(stranger, name="Their Shop"), D3, created_by=stranger.id)
    for report_id in (uuid.uuid4(), theirs.id, "not-a-uuid"):
        assert _publish(client, mapped, report_id).status_code == 404
    assert xero.calls == [] and threads == []


def test_a_person_without_the_publish_right_is_403(client, mapped, xero, threads):
    cashier = make_user("cashier@test.com")
    UserEntity.objects.create(user_id=cashier.id, entity_id=mapped.entity.id, role=EntityRole.CASHIER, approved=True)
    report, *_ = _full_day(mapped)
    resp = _publish(client, mapped, report.id, user=cashier)
    assert resp.status_code == 403
    assert resp.json() == {"error": "You don't have permission to publish reports for this company."}
    assert xero.calls == [] and threads == []


# ---- the worker: a whole day -----------------------------------------------------------------


def test_a_whole_day_goes_into_xero_and_the_report_reads_published(mapped, xero):
    report, first, second = _full_day(mapped)
    Report.objects.filter(id=report.id).update(publishing_status="publishing")

    report = _run(mapped, report)

    assert (report.publishing_status, report.status, report.xero_integrated) == ("completed", "published", True)
    assert report.published_at is not None
    assert list(ReportHistory.objects.filter(report_id=report.id).values_list("action", "user_id")) == [
        ("published", mapped.owner.id)]

    withdrawal, deposit = xero.sent("POST", "BankTransfers")
    assert withdrawal == {"BankTransfers": [{
        "FromBankAccount": {"AccountID": "acc-090", "CurrencyCode": "HKD"},
        "ToBankAccount": {"AccountID": "acc-091", "CurrencyCode": "HKD"},
        "Date": D3.isoformat(), "Amount": 50.0, "Reference": f"MT{D3:%Y%m%d}Withdrawal"}]}
    assert deposit["BankTransfers"][0]["Reference"] == f"MT{D3:%Y%m%d}Deposit"
    assert (deposit["BankTransfers"][0]["FromBankAccount"]["AccountID"], deposit["BankTransfers"][0]["Amount"]) == (
        "acc-091", 150.0)

    invoice, = xero.sent("PUT", "Invoices")
    assert invoice["Invoices"][0]["Contact"] == {"ContactID": "con-cash"}
    assert invoice["Invoices"][0]["LineItems"] == [{"Description": f"MT{D3:%Y%m%d}Cash Sales", "Quantity": 1,
                                                    "UnitAmount": 200.0, "AccountCode": "200", "TaxType": "NONE",
                                                    "LineAmount": 200.0}]

    by_item = {b["bankTransactions"][0]["lineItems"][0]["description"]: b for b in xero.sent("POST", "BankTransactions")}
    coffee, taxi, short = by_item["Coffee"], by_item["Taxi"], by_item["Short by 5"]
    assert coffee["bankTransactions"][0] == {
        "type": "SPEND", "Date": D3.isoformat(), "bankAccount": {"accountID": "acc-091"},
        "Reference": f"MT{D3:%Y%m%d}Expense", "contact": {"contactID": "con-sup"},
        "lineItems": [{"description": "Coffee", "unitAmount": 10.0, "accountCode": "429", "taxType": "NONE"}]}
    assert taxi["bankTransactions"][0]["lineItems"][0]["unitAmount"] == 20.0
    assert short["bankTransactions"][0]["type"] == "SPEND"
    assert short["bankTransactions"][0]["Reference"] == f"MT{D3:%Y%m%d}Discrepancy"
    assert short["bankTransactions"][0]["lineItems"] == [{"description": "Short by 5", "unitAmount": 5.0,
                                                           "accountCode": "420", "taxType": "NONE"}]
    record = publish_record.load_record(report.id)
    record_ids = record["objects"]
    assert set(record["objects"]) == {"withdrawal_from", "invoices", "expenses", "deposit", "discrepancy"}
    assert set(record["objects"]["expenses"]) == {str(first.id), str(second.id)}
    assert record["org"] == ORG
    # Each line's receipt, named after its remarks (``safe_stem``, as Flask names it), on its own transaction.
    assert sorted(b[0] for m, p, b in xero.calls if (m, p) == ("POST", "Files")) == ["COFFEE_RECEIPT.png",
                                                                                    "TAXI_RECEIPT.png"]
    assert sorted(b["ObjectId"] for m, p, b in xero.calls if m == "POST" and p.endswith("/Associations")) == sorted(
        [record_ids["expenses"][str(first.id)]["id"], record_ids["expenses"][str(second.id)]["id"]])


def test_an_over_count_receives_and_a_personal_withdrawal_names_the_director(mapped, xero):
    report = _report(mapped, cash_addition=40, cash_addition_type="personal", discrepancy_amount=3,
                     discrepancy_type="over", discrepancy_reason="Over by 3")

    _run(mapped, report)

    personal, over = xero.sent("POST", "BankTransactions")
    assert personal["bankTransactions"][0]["type"] == "RECEIVE"
    assert personal["bankTransactions"][0]["contact"] == {"contactID": "con-dir"}
    assert personal["bankTransactions"][0]["lineItems"] == [{"description": "Amount due to Olive Director",
                                                              "quantity": 1, "unitAmount": 40.0,
                                                              "accountCode": "880", "taxType": "NONE"}]
    assert over["bankTransactions"][0]["type"] == "RECEIVE"


def test_the_transfers_are_in_the_companys_currency(mapped, xero):
    sgd = CurrencyInfo.objects.create(id=uuid.uuid4(), currency_code="SGD", currency_name="SGD", symbol="$",
                                      decimal_places=2)
    Entity.objects.filter(id=mapped.entity.id).update(currency_id=sgd.id)
    report = _run(mapped, _report(mapped, bank_deposit=150))
    assert xero.sent("POST", "BankTransfers")[0]["BankTransfers"][0]["ToBankAccount"]["CurrencyCode"] == "SGD"
    assert report.publishing_status == "completed"


def test_no_currency_fails_the_transfers_with_a_reason_never_a_silent_hkd(mapped, xero):
    Entity.objects.filter(id=mapped.entity.id).update(currency_id=None)
    report = _run(mapped, _report(mapped, bank_deposit=150))
    assert report.publishing_status == "failed" and xero.sent("POST", "BankTransfers") == []
    failure = ReportHistory.objects.get(report_id=report.id, action="publish_failed")
    assert [i["text"] for i in json.loads(failure.new_value)] == ["Deposit — the company's currency is not chosen"]


# ---- failures --------------------------------------------------------------------------------


def test_a_refused_line_fails_the_publish_with_its_reason_and_the_rest_is_recorded(mapped, xero):
    report, first, second = _full_day(mapped)
    calls = {"n": 0}

    def second_refused(method, url, body, real=xero.request):
        path = xero._path(url)
        if path == "BankTransactions" and body["bankTransactions"][0]["lineItems"][0]["description"] == "Taxi":
            xero.calls.append((method, path, body))
            calls["n"] += 1
            return _Response(400, {"Elements": [{"ValidationErrors": [{"Message": "The contact is archived"}]}]})
        return real(method, url, body)

    xero.request = second_refused
    report = _run(mapped, report)

    assert (report.publishing_status, report.xero_integrated) == ("failed", False)
    items = json.loads(ReportHistory.objects.get(report_id=report.id, action="publish_failed").new_value)
    assert items == [{"text": "Expense 'Taxi' — contact is archived in Xero", "scope": "expense",
                      "expense_id": str(second.id), "deps": [], "failed_contact_id": "con-sup",
                      "module": "expenses"}]
    assert set(publish_record.load_record(report.id)["objects"]["expenses"]) == {str(first.id)}


def test_a_selective_re_publish_retries_only_what_failed(mapped, xero):
    report, first, second = _full_day(mapped)
    _run(mapped, report)  # everything in Xero
    ReportHistory.objects.create(report_id=report.id, action="publish_failed", new_value=json.dumps(
        [{"text": "Expense 'Taxi' — x", "scope": "expense", "expense_id": str(second.id), "module": "expenses"}]))
    xero.calls.clear()

    _run(mapped, report, prior="failed")

    assert [b["bankTransactions"][0]["lineItems"][0]["description"] for b in xero.sent("POST", "BankTransactions")] == [
        "Taxi"]
    assert xero.sent("PUT", "Invoices") == [] and xero.sent("POST", "BankTransfers") == []


def test_a_rejected_token_stops_the_run_with_one_reason(mapped, xero):
    report, *_ = _full_day(mapped)
    xero.answers[("POST", "BankTransfers")] = _Response(401, {"Title": "Unauthorized"})

    report = _run(mapped, report)

    assert report.publishing_status == "failed"
    items = json.loads(ReportHistory.objects.get(report_id=report.id, action="publish_failed").new_value)
    assert [i["text"] for i in items] == ["Xero connection has expired"]
    assert xero.sent("PUT", "Invoices") == []  # stopped after the withdrawal


@pytest.mark.parametrize("reconnect, reason", [(True, "Xero connection has expired"),
                                               (False, "could not reach Xero, please try again")])
def test_a_worker_with_no_token_fails_with_a_reason(mapped, xero, monkeypatch, reconnect, reason):
    def refused(entity_id):
        raise XeroTokenUnavailable(reconnect=reconnect)

    monkeypatch.setattr(pub, "xero_token", refused)
    report = _run(mapped, _report(mapped, bank_deposit=10))
    assert report.publishing_status == "failed"
    assert json.loads(ReportHistory.objects.get(report_id=report.id, action="publish_failed").new_value) == [
        {"text": reason}]


def test_a_crash_never_leaves_the_report_publishing(mapped, xero, monkeypatch):
    report = _report(mapped, bank_deposit=10, publishing_status="publishing")
    monkeypatch.setattr(pub, "publish_report", lambda *a, **k: 1 / 0)
    assert _run(mapped, report).publishing_status == "failed"


# ---- republishing ----------------------------------------------------------------------------


def test_a_republish_updates_what_this_report_made_instead_of_duplicating(mapped, xero):
    report, first, second = _full_day(mapped)
    _run(mapped, report)
    before = publish_record.load_record(report.id)["objects"]
    xero.calls.clear()

    _run(mapped, report)

    paths = [p for m, p, _ in xero.calls if m in ("POST", "PUT") and not p.startswith(("Files", "Contacts"))]
    assert f"BankTransactions/{before['expenses'][str(first.id)]['id']}" in paths
    assert f"Invoices/{before['invoices']['id']}" in paths and ("PUT", "Invoices") not in [(m, p) for m, p, _ in xero.calls]
    # A transfer can't be edited: the recorded one is deleted, then made again.
    assert f"BankTransfers/{before['deposit']['id']}" in paths
    assert xero.sent("POST", f"BankTransfers/{before['deposit']['id']}") == [{"Status": "DELETED"}]


def test_a_record_from_another_xero_organisation_is_ignored(mapped, xero):
    report = _report(mapped, cashsale_total=200)
    publish_record.record_object(report.id, "old-org", "invoices", "inv-old", object_type="INVOICE")
    _run(mapped, report)
    assert len(xero.sent("PUT", "Invoices")) == 1 and xero.sent("POST", "Invoices/inv-old") == []


def test_what_no_longer_applies_comes_out_of_xero(mapped, xero):
    report, first, second = _full_day(mapped)
    _run(mapped, report)
    before = publish_record.load_record(report.id)["objects"]
    taxi = before["expenses"][str(second.id)]["id"]
    second.delete()
    Report.objects.filter(id=report.id).update(bank_deposit=0, expense_total=10)
    xero.calls.clear()

    _run(mapped, report)

    assert xero.sent("POST", f"BankTransactions/{taxi}") == [
        {"bankTransactions": [{"bankTransactionID": taxi, "status": "DELETED"}]}]
    assert xero.sent("POST", f"BankTransfers/{before['deposit']['id']}") == [{"Status": "DELETED"}]
    after = publish_record.load_record(report.id)["objects"]
    assert "deposit" not in after and set(after["expenses"]) == {str(first.id)}


# ---- receipts and contacts -------------------------------------------------------------------


def test_an_unchanged_receipt_is_left_alone_and_a_changed_one_replaced_after(mapped, xero):
    report = _report(mapped, expense_total=10)
    _line(mapped, report, "Coffee", 10, receipt="r/coffee.png")
    _run(mapped, report)
    transaction_id = next(iter(publish_record.load_record(report.id)["objects"]["expenses"].values()))["id"]
    xero.attached[transaction_id] = [{"FileId": "file-old", "Name": "COFFEE_RECEIPT.png", "Size": len(b"receipt-bytes")}]
    xero.calls.clear()

    _run(mapped, report)
    assert [p for m, p, _ in xero.calls if m == "POST" and p.startswith("Files")] == []  # unchanged: nothing sent

    xero.attached[transaction_id] = [{"FileId": "file-old", "Name": "COFFEE_RECEIPT.png", "Size": 1}]
    xero.calls.clear()
    _run(mapped, report)
    order = [(m, p) for m, p, _ in xero.calls if p.startswith("Files")]
    assert order[0] == ("POST", "Files") and order[-1] == ("DELETE", "Files/file-old")


def test_a_supplier_xero_lost_is_made_again_and_the_line_follows(mapped, xero):
    report = _report(mapped, expense_total=10)
    line = _line(mapped, report, "Coffee", 10, contact="con-gone")
    xero.answers[("GET", "Contacts/con-gone")] = _Response(404, {})

    _run(mapped, report)

    made, = xero.sent("POST", "Contacts")
    assert made == {"Contacts": [{"Name": "ABC Supplies", "IsCustomer": False, "IsSupplier": True}]}
    line.refresh_from_db()
    new_id = XeroContactSync.objects.get(id=line.contact_id).xero_contact_id
    assert new_id != "con-gone"
    assert xero.sent("POST", "BankTransactions")[0]["bankTransactions"][0]["contact"] == {"contactID": new_id}


# ---- GET /reports/{id}/publishing-status -----------------------------------------------------


def test_a_publishing_report_says_so(client, mapped, xero):
    report = _report(mapped, publishing_status="publishing")
    body = _status(client, mapped, report.id).json()
    assert (body["publishing_status"], body["status_label"], body["reasons"]) == ("publishing", "Publishing to Xero", [])
    assert xero.calls == []


def test_a_failed_publish_brings_its_reasons(client, mapped, xero):
    report = _report(mapped, publishing_status="failed")
    # The publish's stored reason: a line that has since been removed, so it reads resolved.
    ReportHistory.objects.create(report_id=report.id, action="publish_failed",
                                 new_value=json.dumps([{"text": REASON["text"], "scope": "expense",
                                                        "expense_id": str(uuid.uuid4())}]))
    body = _status(client, mapped, report.id).json()
    assert (body["publishing_status"], body["status_label"], body["status_tone"]) == ("failed", "Publish failed", "danger")
    assert body["reasons"] == [REASON]


def test_a_report_in_xero_reads_published(client, mapped, xero):
    report = _report(mapped, status="published", publishing_status="completed", xero_integrated=True,
                     published_at=datetime(2026, 10, 14, 2, 30, tzinfo=UTC))
    body = _status(client, mapped, report.id).json()
    assert (body["status_label"], body["xero_integrated"], body["published_at"]) == (
        "Published to Xero", True, "2026-10-14T10:30:00+08:00")


def test_another_companys_report_status_is_404(client, mapped, xero):
    stranger = make_user("stranger@test.com")
    theirs = make_report(make_entity(stranger, name="Their Shop"), D3, created_by=stranger.id)
    assert _status(client, mapped, theirs.id).status_code == 404


# ---- Phase 3 of the Xero matching (owner, 2026-11-12): a stale match is refused, named ----------------


def _copies(shop, *guids):
    """The organisation's accounts as the sync's copies hold them."""
    from django.utils import timezone
    from minty_db.models import XeroAccount

    for guid in guids:
        XeroAccount.objects.create(entity_id=shop.entity.id, xero_org_id=ORG, xero_guid=guid, name=guid,
                                   xero_type="EXPENSE", status="ACTIVE", synced_at=timezone.now())


def test_an_account_not_matched_to_this_organisation_is_refused_by_name(client, mapped, xero, threads):
    report, *_ = _full_day(mapped)
    held = AccountInfo.objects.filter(entity_id=mapped.entity.id).exclude(xero_code="429")
    _copies(mapped, *held.values_list("xero_account_id", flat=True))
    AccountInfo.objects.filter(entity_id=mapped.entity.id, xero_code="429").update(xero_account_id="old-429")

    resp = _publish(client, mapped, report.id)

    assert resp.status_code == 409
    assert resp.json() == {"error": "Account 429 (429) isn't matched to your Xero organisation. Match it in "
                                    "Settings, under Integration, then publish again."}
    assert threads == [] and Report.objects.get(id=report.id).publishing_status != "publishing"


def test_an_account_archived_in_xero_is_refused_by_name(client, mapped, xero, threads):
    from minty_db.models import XeroAccount

    report, *_ = _full_day(mapped)
    _copies(mapped, *AccountInfo.objects.filter(entity_id=mapped.entity.id).values_list("xero_account_id", flat=True))
    XeroAccount.objects.filter(entity_id=mapped.entity.id, xero_guid="acc-429").update(status="ARCHIVED")

    resp = _publish(client, mapped, report.id)

    assert resp.status_code == 409
    assert resp.json() == {"error": "Account 429 (429) is archived in Xero. Restore it in Xero or choose another "
                                    "account, then publish again."}


def test_a_supplier_of_an_older_organisation_is_not_refused(client, mapped, xero, threads):
    """The census (production's data, 2026-11-12): the publish re-creates a supplier Xero doesn't
    know, as it always has, so an older organisation's supplier isn't a reason to refuse."""
    report, *_ = _full_day(mapped)
    XeroContactSync.objects.filter(entity_id=mapped.entity.id, xero_contact_id="con-sup").update(
        xero_org_id="org-old")
    assert _publish(client, mapped, report.id).status_code == 202


def test_several_stale_matches_are_listed_one_a_line(client, mapped, xero, threads):
    report, *_ = _full_day(mapped)
    _copies(mapped, "acc-091")
    resp = _publish(client, mapped, report.id)
    assert resp.status_code == 409
    lines = resp.json()["error"].split("\n")
    assert len(lines) > 1 and all("isn't matched to your Xero organisation" in line for line in lines)


@pytest.mark.parametrize("reason", ["first", "new_accounts", "new_org"])
def test_an_open_matching_holds_every_publish(client, mapped, xero, threads, reason):
    Entity.objects.filter(id=mapped.entity.id).update(xero_matching=reason)
    report, *_ = _full_day(mapped)
    resp = _publish(client, mapped, report.id)
    assert (resp.status_code, resp.json()) == (409, {"error": pub.STILL_MATCHING})


def test_a_change_of_matches_in_progress_does_not_hold_a_publish(client, mapped, xero, threads):
    Entity.objects.filter(id=mapped.entity.id).update(xero_matching="change")
    report, *_ = _full_day(mapped)
    assert _publish(client, mapped, report.id).status_code == 202

