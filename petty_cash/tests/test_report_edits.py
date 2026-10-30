"""A submitted report's details that move no balance (``services/report_edits.py``): the difference
reason, the withdrawal and an expense line - Flask's ``tests/test_report_edits_state.py`` and the last
section of ``tests/test_petty_cash_api_report.py``, restated. FIXED tests name what Flask's service
changed on purpose (2026-10-16)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from minty_db.enums import SystemRole
from minty_db.models import (
    AccountInfo,
    Attachment,
    EntityAccountXero,
    Report,
    ReportExpense,
    ReportExpenseAttachment,
    XeroContactSync,
)

from petty_cash.tests.conftest import headers, make_entity, make_report, make_user, url
from petty_cash.tests.test_close import D3, _ready, _submit
from petty_cash.tests.test_expenses import JPEG, PNG, lists, s3  # noqa: F401  (fixtures)

pytestmark = pytest.mark.django_db

OLD_KEY = "expenses/x/OLD_RECEIPT.png"


@pytest.fixture
def day(client, shop, notes, lists, s3):  # noqa: F811
    """A submitted day whose one line is Advertising (acc-0001), ABC Supplies (con-0001), 15,
    "Flyers", with one receipt in the bucket."""
    report = _ready(shop, notes)
    assert _submit(client, shop).status_code == 200
    line = ReportExpense.objects.get(report_id=report.id)
    ReportExpense.objects.filter(id=line.id).update(
        remarks="Flyers", account_id=AccountInfo.objects.get(xero_account_id="acc-0001").id,
        contact_id=XeroContactSync.objects.get(xero_contact_id="con-0001").id,
    )
    s3.objects[OLD_KEY] = PNG
    receipt = Attachment.objects.create(file_path=OLD_KEY, original_name="old.png", mime_type="image/png")
    ReportExpenseAttachment.objects.create(report_expense_id=line.id, attachment_id=receipt.id, sort_order=0,
                                           xero_attachment_id="")
    return Report.objects.get(id=report.id), ReportExpense.objects.get(id=line.id)


def _set(report, **fields) -> None:
    Report.objects.filter(id=report.id).update(**fields)


def _row(report) -> Report:
    return Report.objects.get(id=report.id)


def _get(client, shop, report_id, user=None):
    return client.get(url(shop.entity.id, f"/reports/{report_id}"), headers=headers((user or shop.owner).id))


def _put(client, shop, report_id, tail, body, user=None):
    return client.put(url(shop.entity.id, f"/reports/{report_id}{tail}"), body, content_type="application/json",
                      headers=headers((user or shop.owner).id))


def _reason(client, shop, report_id, reason, user=None):
    return _put(client, shop, report_id, "/difference-reason", {"reason": reason}, user)


def _withdrawal(client, shop, report_id, kind, user=None):
    return _put(client, shop, report_id, "/withdrawal", {"withdrawal_type": kind}, user)


def _edit_line(client, shop, report_id, line_id, user=None, **form):
    return client.patch(url(shop.entity.id, f"/reports/{report_id}/expense-lines/{line_id}"),
                        encode_multipart(BOUNDARY, form), content_type=MULTIPART_CONTENT,
                        headers=headers((user or shop.owner).id))


def _file(name, body):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, body, content_type="image/png")


def _keys(line) -> list[str]:
    ids = ReportExpenseAttachment.objects.filter(report_expense_id=line.id).order_by("sort_order")
    paths = dict(Attachment.objects.values_list("id", "file_path"))
    return [paths[link.attachment_id] for link in ids]


def _line(line) -> ReportExpense:
    return ReportExpense.objects.get(id=line.id)


# --- the Daily Report says what may be edited ----------------------------------------------------------


def test_the_daily_report_offers_the_details_and_what_a_line_may_become(client, shop, day):
    report, _ = day

    body = _get(client, shop, report.id).json()

    assert body["permissions"]["edit_details"] is True
    assert body["options"] == {
        "suppliers": [{"id": "con-0001", "name": "ABC Supplies"}, {"id": "con-0002", "name": "Taxi Co"}],
        "accounts": [{"id": "acc-0001", "code": "400", "name": "Advertising"}],  # acc-0002 is switched off
    }
    assert list(body)[-2:] == ["permissions", "options"]


def test_an_older_report_s_details_may_be_edited(client, shop, notes):
    older = make_report(shop.entity, D3 - timedelta(days=1), created_by=shop.owner.id, opening_balance=0,
                        closing_balance=100, completed_sections=["submitted"])
    _ready(shop, notes)
    assert _submit(client, shop).status_code == 200

    body = _get(client, shop, older.id).json()
    assert body["permissions"]["edit"] is False and body["permissions"]["edit_details"] is True
    resp = _reason(client, shop, older.id, "late note")
    assert resp.status_code == 200, resp.json()
    assert _row(older).discrepancy_reason == "late note"


@pytest.mark.parametrize("fields, message", [
    ({"status": "draft"}, "This report is still a draft. Change it in the Daily Closing."),
    ({"publishing_status": "publishing"}, "Xero is still publishing this report. Wait until it finishes, then edit it."),
])
def test_fixed_a_draft_or_a_report_being_published_keeps_its_details(client, shop, day, s3, fields, message):  # noqa: F811
    report, line = day
    _set(report, discrepancy_reason="old", **fields)

    body = _get(client, shop, report.id).json()
    assert body["permissions"]["edit_details"] is False and body["options"] is None
    for resp in (_reason(client, shop, report.id, "new"), _withdrawal(client, shop, report.id, "company"),
                 _edit_line(client, shop, report.id, line.id, remarks="Posters", receipt=_file("new.png", PNG))):
        assert resp.status_code == 409 and resp.json() == {"error": message}
    assert _row(report).discrepancy_reason == "old" and _line(line).remarks == "Flyers"
    assert set(s3.objects) == {OLD_KEY}  # nothing uploaded for a refused edit


def test_someone_without_the_right_is_refused(client, shop, day):
    """A superuser who is not a member may read the report but write nothing."""
    report, line = day
    boss = make_user("boss@test.com", system_role=SystemRole.SUPERADMIN)

    body = _get(client, shop, report.id, user=boss).json()
    assert body["permissions"]["edit_details"] is False and body["options"] is None
    refused = "It looks like you don't have permission to edit this report."
    for resp in (_reason(client, shop, report.id, "new", user=boss),
                 _withdrawal(client, shop, report.id, "company", user=boss),
                 _edit_line(client, shop, report.id, line.id, user=boss, remarks="Posters")):
        assert resp.status_code == 403 and resp.json() == {"error": refused}


def test_another_companys_or_an_unknown_report_is_404(client, shop, day):
    theirs = make_report(make_entity(shop.owner, name="Other Shop"), D3, created_by=shop.owner.id)

    for report_id in (uuid.uuid4(), "nope", theirs.id):
        for resp in (_reason(client, shop, report_id, "x"), _withdrawal(client, shop, report_id, "company")):
            assert resp.status_code == 404 and resp.json() == {"error": "Hmm, I couldn't find that report."}


# --- the difference reason -----------------------------------------------------------------------------


def test_the_reason_is_saved_and_the_report_leaves_xero(client, shop, day):
    report, _ = day
    _set(report, discrepancy_amount=-5, discrepancy_type="short", discrepancy_reason="old", xero_integrated=True,
         publishing_status="completed", status="published")

    resp = _reason(client, shop, report.id, "  recounted  ")

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["cash"]["difference"]["reason"] == "recounted"
    assert body["status_label"] == "Submitted"  # out of Xero until it is published again
    row = _row(report)
    assert (row.discrepancy_reason, row.status, row.xero_integrated) == ("recounted", "submitted", False)
    assert row.publishing_status == "completed"  # kept: a republish warns about duplicates


def test_no_difference_may_have_no_reason(client, shop, day):
    report, _ = day
    _set(report, discrepancy_reason="note")

    assert _reason(client, shop, report.id, "").status_code == 200
    assert _row(report).discrepancy_reason == ""


@pytest.mark.parametrize("kind", ["short", "over"])
def test_fixed_a_difference_keeps_a_reason(client, shop, day, kind):
    report, _ = day
    _set(report, discrepancy_amount=-5, discrepancy_type=kind, discrepancy_reason="old")

    resp = _reason(client, shop, report.id, "  ")

    assert resp.status_code == 422
    assert resp.json() == {"error": f"The cash was {kind}, so the reason is still needed. Add the reason to save it."}
    assert _row(report).discrepancy_reason == "old"


def test_fixed_a_long_reason_is_refused_not_cut(client, shop, day):
    report, _ = day
    _set(report, discrepancy_reason="old")

    resp = _reason(client, shop, report.id, "x" * 301)

    assert resp.status_code == 422 and resp.json() == {"error": "Keep the reason to 300 characters or fewer."}
    assert _row(report).discrepancy_reason == "old"
    assert _reason(client, shop, report.id, "x" * 300).status_code == 200


# --- the withdrawal ------------------------------------------------------------------------------------


def test_the_withdrawal_source_is_saved(client, shop, day):
    report, _ = day
    _set(report, cash_addition_type="personal", xero_integrated=True)

    resp = _withdrawal(client, shop, report.id, "company")

    assert resp.status_code == 200, resp.json()
    assert resp.json()["cash"]["cash_addition_type"] == "company"
    assert _row(report).cash_addition_type == "company" and _row(report).xero_integrated is False
    bad = _withdrawal(client, shop, report.id, "bank")
    assert bad.status_code == 400 and bad.json() == {"error": "That withdrawal type doesn't look quite right to me."}
    assert _row(report).cash_addition_type == "company"


# --- an expense line -----------------------------------------------------------------------------------


def test_a_line_s_details_change_and_its_amount_does_not(client, shop, day):
    report, line = day
    _set(report, xero_integrated=True, publishing_status="completed")

    resp = _edit_line(client, shop, report.id, line.id, remarks="Posters", supplier_id="con-0002", amount="99")

    assert resp.status_code == 200, resp.json()
    [shown] = resp.json()["expenses"]["lines"]
    assert (shown["remarks"], shown["supplier"], shown["amount"]) == (
        "Posters", {"id": "con-0002", "name": "Taxi Co"}, 15)
    row = _line(line)
    assert row.contact_id == XeroContactSync.objects.get(xero_contact_id="con-0002").id and row.amount == 15
    assert row.item == "Advertising" and _keys(line) == [OLD_KEY]  # untouched
    assert _row(report).xero_integrated is False


def test_fixed_a_new_account_renames_the_line(client, shop, day):
    report, line = day
    fees = AccountInfo.objects.create(entity_id=shop.entity.id, xero_account_id="acc-0003", xero_code="404",
                                      name="Bank Fees", type="EXPENSE", status="ACTIVE")
    EntityAccountXero.objects.create(account_id=fees.id, name="Bank Fees", type="EXPENSE", is_active=True)

    resp = _edit_line(client, shop, report.id, line.id, account_id="acc-0003")

    assert resp.status_code == 200, resp.json()
    assert (_line(line).item, _line(line).account_id, _line(line).remarks) == ("Bank Fees", fees.id, "Flyers")
    assert resp.json()["expenses"]["lines"][0]["account"] == {"id": "acc-0003", "code": "404", "name": "Bank Fees"}


@pytest.mark.parametrize("form, message", [
    ({"account_id": "acc-elsewhere"}, "Choose an account code from the list."),
    ({"account_id": "acc-0002"}, "Choose an account code from the list."),  # switched off for Petty Cash
    ({"supplier_id": "con-elsewhere"}, "Choose a supplier from the list."),
    ({"remarks": "x" * 301}, "Keep the description to 300 characters or fewer."),
])
def test_fixed_an_account_or_supplier_must_be_the_company_s(client, shop, day, form, message):
    report, line = day
    other = make_entity(shop.owner, name="Other Shop")
    XeroContactSync.objects.create(entity_id=other.id, xero_contact_id="con-elsewhere", name="Elsewhere")

    resp = _edit_line(client, shop, report.id, line.id, **form)

    assert resp.status_code == 422 and resp.json() == {"error": message}
    row = _line(line)
    assert (row.item, row.remarks, row.contact_id) == (
        "Advertising", "Flyers", XeroContactSync.objects.get(xero_contact_id="con-0001").id)


@pytest.mark.parametrize("body, message", [
    (b"just text", "Attach a PDF, JPEG or PNG file."),
    (b"", "That file is empty. Attach the receipt again."),
])
def test_fixed_a_new_receipt_is_checked_as_the_expenses_step_checks_it(client, shop, day, s3, body, message):  # noqa: F811
    report, line = day

    resp = _edit_line(client, shop, report.id, line.id, receipt=_file("notes.png", body))

    assert resp.status_code == 422 and resp.json() == {"error": message}
    assert _keys(line) == [OLD_KEY] and set(s3.objects) == {OLD_KEY}


def test_a_receipt_over_10_mb_is_refused(client, shop, day, settings):
    report, line = day
    settings.MAX_RECEIPT_BYTES = 32

    resp = _edit_line(client, shop, report.id, line.id, receipt=_file("big.png", PNG + b"\x00" * 32))

    assert resp.status_code == 422 and resp.json() == {"error": "Attach a file of 10 MB or less."}


def test_one_receipt_per_line(client, shop, day):
    report, line = day

    resp = _edit_line(client, shop, report.id, line.id, receipt=[_file("a.png", PNG), _file("b.png", PNG)])

    assert resp.status_code == 422 and resp.json() == {"error": "Attach one receipt per expense."}


def test_fixed_a_new_receipt_replaces_the_line_s_and_the_old_file_goes_after_the_commit(
        client, shop, day, s3, django_capture_on_commit_callbacks):  # noqa: F811
    report, line = day
    old = Attachment.objects.get(file_path=OLD_KEY)

    with django_capture_on_commit_callbacks(execute=True):
        resp = _edit_line(client, shop, report.id, line.id, receipt=_file("new.png", JPEG))

    assert resp.status_code == 200, resp.json()
    [key] = _keys(line)
    assert key != OLD_KEY and key.startswith(f"expenses/{report.id}/") and key in s3.objects
    assert OLD_KEY not in s3.objects and not Attachment.objects.filter(id=old.id).exists()
    new = Attachment.objects.get(file_path=key)
    assert (new.original_name, new.mime_type, new.uploaded_by) == ("new.png", "image/jpeg", shop.owner.id)
    [shown] = resp.json()["expenses"]["lines"][0]["receipts"]
    assert shown["id"] == str(new.id) and shown["name"] == "new.png"


def test_a_file_another_line_still_uses_stays(client, shop, day, s3, django_capture_on_commit_callbacks):  # noqa: F811
    report, line = day
    twin = ReportExpense.objects.create(report_id=report.id, item="Twin", amount=0)
    ReportExpenseAttachment.objects.create(report_expense_id=twin.id, sort_order=0, xero_attachment_id="",
                                           attachment_id=Attachment.objects.get(file_path=OLD_KEY).id)

    with django_capture_on_commit_callbacks(execute=True):
        resp = _edit_line(client, shop, report.id, line.id, receipt=_file("new.png", PNG))

    assert resp.status_code == 200, resp.json()
    assert OLD_KEY in s3.objects and Attachment.objects.filter(file_path=OLD_KEY).exists()
    assert _keys(twin) == [OLD_KEY] and _keys(line) != [OLD_KEY]


def test_a_failed_edit_keeps_the_old_receipt_and_removes_the_new_upload(client, shop, day, s3, monkeypatch,  # noqa: F811
                                                                       django_capture_on_commit_callbacks):
    report, line = day

    def fail(_report):
        raise RuntimeError("the report could not be marked")

    monkeypatch.setattr("petty_cash.services.report_edits.mark_diverged_from_xero", fail)
    with django_capture_on_commit_callbacks(execute=True):
        resp = _edit_line(client, shop, report.id, line.id, remarks="Posters", receipt=_file("new.png", PNG))

    assert resp.status_code == 500
    assert _keys(line) == [OLD_KEY] and _line(line).remarks == "Flyers"
    assert set(s3.objects) == {OLD_KEY}  # the new upload went again, the old file stayed


def test_an_expense_line_of_another_report_or_company_is_not_found(client, shop, day):
    report, line = day
    older = make_report(shop.entity, D3 - timedelta(days=1), created_by=shop.owner.id)
    theirs = make_report(make_entity(shop.owner, name="Other Shop"), D3, created_by=shop.owner.id)
    their_line = ReportExpense.objects.create(report_id=theirs.id, item="Theirs", amount=1)

    for report_id, line_id in ((older.id, line.id), (report.id, uuid.uuid4()), (report.id, "nope"),
                               (report.id, their_line.id), (theirs.id, their_line.id)):
        resp = _edit_line(client, shop, report_id, line_id, remarks="x")
        assert resp.status_code == 404 and resp.json() == {"error": "Hmm, I couldn't find that expense."}
    assert _line(line).remarks == "Flyers"
