"""The Expenses step (``petty_cash/routes/expenses.py``) - Flask's
``tests/test_petty_cash_api_expenses.py`` expectations, restated. The bucket is an in-memory fake at
the S3 client (``petty_cash/files/storage.client``)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from petty_cash.days import today
from petty_cash.files import storage
from petty_cash.tests.conftest import WEB, headers, make_entity, make_report, make_user, url
from shared_models.enums import EntityRole
from shared_models.models import (
    AccountInfo,
    Attachment,
    EntityAccountXero,
    Report,
    ReportExpense,
    ReportExpenseAttachment,
    ReportHistory,
    UserEntity,
    XeroContactSync,
)

pytestmark = pytest.mark.django_db
D3 = today() - timedelta(days=1)
PNG = b"\x89PNG\r\n\x1a\n" + b"small-png-bytes"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


class FakeS3:
    """The S3 client the storage module calls, in memory."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def upload_fileobj(self, fileobj, bucket, key, **kwargs):
        self.objects[key] = fileobj.read()

    def get_object(self, *, Bucket, Key, **kwargs):
        return {"Body": BytesIO(self.objects[Key])}

    def delete_objects(self, *, Bucket, Delete, **kwargs):
        for obj in Delete["Objects"]:
            self.objects.pop(obj["Key"], None)
            self.deleted.append(obj["Key"])


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(storage, "client", lambda: fake)
    return fake


@pytest.fixture
def lists(shop):
    """Two synced suppliers; one account on for Petty Cash, one switched off."""
    XeroContactSync.objects.create(entity_id=shop.entity.id, xero_contact_id="con-0002", name="Taxi Co")
    XeroContactSync.objects.create(entity_id=shop.entity.id, xero_contact_id="con-0001", name="ABC Supplies")
    on = AccountInfo.objects.create(entity_id=shop.entity.id, xero_account_id="acc-0001", xero_code="400",
                                    name="Advertising", type="EXPENSE", status="ACTIVE")
    off = AccountInfo.objects.create(entity_id=shop.entity.id, xero_account_id="acc-0002", xero_code="410",
                                     name="Cleaning", type="EXPENSE", status="ACTIVE")
    EntityAccountXero.objects.create(account_id=on.id, name="Advertising", type="EXPENSE", is_active=True)
    EntityAccountXero.objects.create(account_id=off.id, name="Cleaning", type="EXPENSE", is_active=False)


def _draft(shop, **fields):
    values = {"opening_balance": 100, "cash_addition": 20, "adjusted_opening_balance": 120, "cashsale_total": 50,
              "closing_balance": 170, "bank_deposit": 5, "completed_sections": ["opening", "sales"],
              "current_section": "expenses"}
    values.update(fields)
    return make_report(shop.entity, D3, status=values.pop("status", "draft"), created_by=shop.owner.id, **values)


def _get(client, shop, user=None, day=D3):
    query = {"transaction_date": day.isoformat()} if day else {}
    return client.get(url(shop.entity.id, "/reports/expenses"), query, headers=headers((user or shop.owner).id))


def _add(client, shop, user=None, **over):
    form = {"transaction_date": D3.isoformat(), "amount": "1,012.50", "remarks": "Flyers",
            "supplier_id": "con-0001", "account_id": "acc-0001", "receipt": PNG}
    form.update(over)
    receipt = form.pop("receipt")
    if receipt is not None:
        form["receipt"] = SimpleUploadedFile("till-slip.jpg", receipt, content_type="image/jpeg")
    return client.post(url(shop.entity.id, "/reports/expenses/lines"), form, headers=headers((user or shop.owner).id))


def _line(shop, draft, s3, amount="1012.50", *, receipt=JPEG, name="till-slip.jpg"):
    """A stored line as an earlier add left it."""
    line = ReportExpense.objects.create(
        report_id=draft.id, item="Advertising", amount=Decimal(amount), remarks="Flyers",
        account_id=AccountInfo.objects.get(xero_account_id="acc-0001").id,
        contact_id=XeroContactSync.objects.get(xero_contact_id="con-0001").id,
    )
    key = f"expenses/{draft.id}/{line.id}.jpg"
    s3.objects[key] = receipt
    attachment = Attachment.objects.create(original_name=name, stored_name=f"{line.id}.jpg", file_path=key,
                                           file_extension="jpg", mime_type="image/jpeg")
    ReportExpenseAttachment.objects.create(report_expense_id=line.id, attachment_id=attachment.id, sort_order=0)
    return line, attachment


# --- GET ---------------------------------------------------------------------------------------------


def test_the_page_brings_the_lines_and_what_a_line_may_use(client, shop, lists, s3):
    draft = _draft(shop)
    line, _ = _line(shop, draft, s3)

    resp = _get(client, shop)

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["transaction_date"] == D3.isoformat() and body["currency_code"] == "HKD"
    assert body["draft"]["id"] == draft.id and body["draft"]["completed_sections"] == ["opening", "sales"]
    assert body["suppliers"] == [{"id": "con-0001", "name": "ABC Supplies"}, {"id": "con-0002", "name": "Taxi Co"}]
    assert body["accounts"] == [{"id": "acc-0001", "code": "400", "name": "Advertising"}]
    assert body["total"] == 1012.5
    [only] = body["lines"]
    assert only["id"] == line.id and only["item"] == "Advertising" and only["amount"] == 1012.5
    assert only["remarks"] == "Flyers"
    assert only["supplier"] == {"id": "con-0001", "name": "ABC Supplies"}
    assert only["account"] == {"id": "acc-0001", "code": "400", "name": "Advertising"}
    assert [(r["name"], r["mime_type"]) for r in only["receipts"]] == [("till-slip.jpg", "image/jpeg")]
    assert body["permissions"] == {"create_supplier": True}


def test_with_no_draft_the_page_goes_back_to_opening(client, shop, lists):
    resp = _get(client, shop)

    assert resp.status_code == 409
    assert resp.json() == {"error": "I don't see a draft yet - let's start with the opening entry.", "to": "opening"}


def test_a_missing_date_and_a_member_without_the_right_are_refused(client, shop, lists, s3):
    _draft(shop)
    assert _get(client, shop, day=None).status_code == 422
    base = make_user("base@test.com")
    UserEntity.objects.create(user_id=base.id, entity_id=shop.entity.id, role=EntityRole.ENTITY_BASE, approved=True)

    assert _get(client, shop, user=base).status_code == 403
    assert _add(client, shop, user=base).status_code == 403
    assert s3.objects == {}


# --- adding a line -----------------------------------------------------------------------------------


def test_a_line_is_stored_with_its_receipt_as_its_bytes_say(client, shop, lists, s3):
    draft = _draft(shop)

    resp = _add(client, shop)  # a PNG named .jpg and sent as image/jpeg

    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["total"] == 1012.5
    assert body["line"]["receipts"][0]["mime_type"] == "image/png"
    assert body["line"]["receipts"][0]["name"] == "till-slip.jpg"
    row = Report.objects.get(id=draft.id)
    assert row.expense_total == Decimal("1012.50")
    assert row.closing_balance == Decimal("100") + 20 + 50 - Decimal("1012.50") - 5
    line = ReportExpense.objects.get(report_id=draft.id)
    assert line.item == "Advertising" and line.remarks == "Flyers"
    assert line.contact_id == XeroContactSync.objects.get(xero_contact_id="con-0001").id
    assert line.account_id == AccountInfo.objects.get(xero_account_id="acc-0001").id
    [key] = s3.objects
    assert key.startswith(f"expenses/{draft.id}/") and key.endswith("_ADVERTISING_1012.jpg")
    history = list(ReportHistory.objects.filter(report_id=draft.id).values_list("action", "new_value"))
    assert history == [("added", "Added expense: Advertising ($1012.5)")]


def test_two_lines_alike_keep_their_own_receipts(client, shop, lists, s3):
    _draft(shop)

    _add(client, shop, amount="12.40")
    _add(client, shop, amount="12.10")

    assert len(s3.objects) == 2  # the second did not overwrite the first


@pytest.mark.parametrize("over, message", [
    ({"amount": "0"}, "Enter the amount as a number above 0"),
    ({"amount": "-5"}, "Enter the amount as a number above 0"),
    ({"amount": "twelve"}, "Enter the amount as a number above 0"),
    ({"amount": "100000000000"}, "up to 99,999,999,999.99"),
    ({"remarks": "x" * 301}, "Keep the description to 300 characters or fewer."),
    ({"supplier_id": "con-unknown"}, "Choose a supplier from the list."),
    ({"supplier_id": ""}, "Choose a supplier from the list."),
    ({"account_id": "acc-0002"}, "Choose an account code from the list."),  # switched off
    ({"receipt": None}, "Attach the receipt to add this expense."),
    ({"receipt": b"GIF89a-not-allowed"}, "Attach a PDF, JPEG or PNG file."),
    ({"receipt": b""}, "That file is empty. Attach the receipt again."),
    ({"receipt": b"%PDF" + b"0" * (10 * 1024 * 1024)}, "Attach a file of 10 MB or less."),
    ({"transaction_date": "14/10/2026"}, "That date doesn't look quite right!"),
])
def test_bad_input_is_a_422_and_nothing_is_stored(client, shop, lists, s3, over, message):
    draft = _draft(shop)

    resp = _add(client, shop, **over)

    assert resp.status_code == 422
    assert message in resp.json()["error"]
    assert not ReportExpense.objects.filter(report_id=draft.id).exists()
    assert s3.objects == {}


def test_a_line_without_a_draft_goes_back_to_opening(client, shop, lists, s3):
    resp = _add(client, shop)

    assert resp.status_code == 409 and resp.json()["to"] == "opening"
    assert s3.objects == {}


def test_a_line_on_a_submitted_day_is_refused(client, shop, lists, s3):
    _draft(shop, status="submitted")

    resp = _add(client, shop)

    assert resp.status_code == 409  # no draft for the day
    assert s3.objects == {}


def test_a_failed_save_leaves_no_receipt_behind(client, shop, lists, s3, monkeypatch):
    from petty_cash.services import expenses as service

    _draft(shop)

    def broken(*args, **kwargs):
        raise RuntimeError("the row did not save")

    monkeypatch.setattr(service, "recompute_totals", broken)

    resp = _add(client, shop)

    assert resp.status_code == 500
    assert resp.json() == {"error": "Something went wrong on our side. Please try again."}
    assert s3.objects == {} and len(s3.deleted) == 1
    assert not ReportExpense.objects.exists()


# --- deleting a line ---------------------------------------------------------------------------------


def test_deleting_a_line_answers_the_new_total(client, shop, lists, s3, django_capture_on_commit_callbacks):
    draft = _draft(shop)
    _line(shop, draft, s3, "15.00")
    doomed, attachment = _line(shop, draft, s3, "40.00")
    Report.objects.filter(id=draft.id).update(expense_total=55)

    with django_capture_on_commit_callbacks(execute=True):  # the files go only once the rows are committed
        resp = client.delete(url(shop.entity.id, f"/expense-lines/{doomed.id}"), headers=headers(shop.owner.id))

    assert resp.status_code == 200 and resp.json() == {"total": 15.0}
    assert Report.objects.get(id=draft.id).expense_total == 15
    assert not ReportExpense.objects.filter(id=doomed.id).exists()
    assert not Attachment.objects.filter(id=attachment.id).exists()
    assert s3.deleted == [attachment.file_path]
    assert ReportHistory.objects.filter(report_id=draft.id, action="deleted").count() == 1


def test_a_line_of_another_company_or_a_submitted_report_is_left_alone(client, shop, lists, s3):
    other_owner = make_user("other@test.com")
    other = make_entity(other_owner, name="Other Co")
    theirs_report = make_report(other, D3, status="draft")
    theirs = ReportExpense.objects.create(report_id=theirs_report.id, item="X", amount=1)

    assert client.delete(url(shop.entity.id, f"/expense-lines/{theirs.id}"),
                         headers=headers(shop.owner.id)).status_code == 404
    assert client.delete(url(shop.entity.id, "/expense-lines/not-a-uuid"),
                         headers=headers(shop.owner.id)).status_code == 404

    submitted = _draft(shop, status="submitted")
    line, _ = _line(shop, submitted, s3)
    resp = client.delete(url(shop.entity.id, f"/expense-lines/{line.id}"), headers=headers(shop.owner.id))

    assert resp.status_code == 409
    assert resp.json()["error"] == "This report isn't a draft anymore, so I can't change its expenses."
    assert ReportExpense.objects.filter(id=line.id).exists() and s3.deleted == []


# --- a receipt ---------------------------------------------------------------------------------------


def test_a_receipt_comes_back_by_its_id_with_the_type_its_bytes_say(client, shop, lists, s3):
    draft = _draft(shop)
    line, attachment = _line(shop, draft, s3, receipt=PNG)  # stored as image/jpeg, the bytes are a PNG

    resp = client.get(url(shop.entity.id, f"/expense-lines/{line.id}/receipts/{attachment.id}"),
                      headers=headers(shop.owner.id))

    assert resp.status_code == 200
    assert resp.content == PNG and resp["Content-Type"] == "image/png"
    assert resp["X-Content-Type-Options"] == "nosniff" and resp["Content-Disposition"] == "inline"
    assert resp["Access-Control-Allow-Origin"] == WEB
    wrong = client.get(url(shop.entity.id, f"/expense-lines/{line.id}/receipts/{line.id}"),
                       headers=headers(shop.owner.id))
    assert wrong.status_code == 404 and wrong.json() == {"error": "I couldn't find that file."}


# --- the step's save ---------------------------------------------------------------------------------


def test_save_next_marks_expenses_done_and_save_exit_records_nothing(client, shop, lists):
    draft = _draft(shop)

    def put(action):
        return client.put(url(shop.entity.id, "/reports/expenses"),
                          {"transaction_date": D3.isoformat(), "action": action},
                          content_type="application/json", headers=headers(shop.owner.id))

    exit_ = put("exit")
    assert exit_.status_code == 200 and exit_.json()["next"] == "dashboard"
    assert Report.objects.get(id=draft.id).completed_sections == ["opening", "sales"]

    nxt = put("next")
    assert nxt.status_code == 200
    assert nxt.json()["next"] == "deposit" and nxt.json()["current_section"] == "deposit"
    assert Report.objects.get(id=draft.id).completed_sections == ["opening", "sales", "expenses"]

    assert put("later").status_code == 422
