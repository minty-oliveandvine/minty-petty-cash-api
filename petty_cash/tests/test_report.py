"""A report after it is submitted (``petty_cash/routes/report.py``): the Daily Report, reopen, delete
and the share link - Flask's ``tests/test_petty_cash_api_report.py`` and
``tests/test_report_actions_state.py`` expectations, restated. FIXED / RULE tests name what Flask's
service changed on purpose (2026-10-15)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from datetime import timedelta

import pytest
from django.conf import settings

from petty_cash.tests.conftest import headers, make_entity, make_report, make_user, url
from petty_cash.tests.test_close import D3, _ready, _stock, _submit
from shared_models.enums import EntityRole, SaleType
from shared_models.models import (
    Attachment,
    Report,
    ReportCashCount,
    ReportExpense,
    ReportExpenseAttachment,
    ReportHistory,
    ReportSale,
    SaleInfo,
    ShareLink,
    UserEntity,
    XeroReportSync,
)

pytestmark = pytest.mark.django_db


def _submitted(client, shop, notes, **fields) -> Report:
    report = _ready(shop, notes, **fields)
    assert _submit(client, shop).status_code == 200
    return Report.objects.get(id=report.id)


def _older(shop, **fields) -> Report:
    values = {"opening_balance": 0, "closing_balance": 100, "completed_sections": ["submitted"]}
    values.update(fields)
    return make_report(shop.entity, D3 - timedelta(days=1), created_by=shop.owner.id, **values)


def _set(report, **fields) -> None:
    Report.objects.filter(id=report.id).update(**fields)


def _get(client, shop, report_id, user=None):
    return client.get(url(shop.entity.id, f"/reports/{report_id}"), headers=headers((user or shop.owner).id))


def _post(client, shop, report_id, tail, user=None):
    return client.post(url(shop.entity.id, f"/reports/{report_id}{tail}"), headers=headers((user or shop.owner).id))


def _delete(client, shop, report_id, user=None):
    return client.delete(url(shop.entity.id, f"/reports/{report_id}"), headers=headers((user or shop.owner).id))


# --- the Daily Report --------------------------------------------------------------------------------


def test_the_daily_report_brings_the_day_and_what_may_be_done(client, shop, notes):
    report = _submitted(client, shop, notes)

    resp = _get(client, shop, report.id)

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["report_id"] == str(report.id) and body["transaction_date"] == D3.isoformat()
    assert (body["status"], body["status_label"], body["status_tone"]) == ("submitted", "Submitted", "info")
    assert body["is_latest"] is True and body["was_published"] is False
    assert body["kpis"] == {"total_sales": 0, "total_expenses": 15, "cash_position": 55}
    assert body["expenses"]["total"] == 15 and body["expenses"]["lines"][0]["item"] == "Advertising"
    assert body["cash"]["expected"] == 55 and body["cash"]["counted"] == 55
    assert body["cash"]["difference"] == {"amount": 0, "kind": "none", "reason": ""}
    assert body["closing_balance"] == 55 and body["submitted_at"].endswith("+08:00")
    assert body["submitted_by"] == "Olive Owner" and body["company_name"] == "Acme Shop"
    assert body["permissions"] == {"edit": True, "delete": True, "publish": True, "share": True, "edit_details": True}
    assert body["options"] == {"suppliers": [], "accounts": []}


def test_the_steps_are_not_taken_for_a_report_id(client, shop, notes):
    """``/reports/{id}`` is mounted last: ``/reports/opening`` is still the Opening step."""
    resp = client.get(url(shop.entity.id, "/reports/opening"), headers=headers(shop.owner.id))

    assert resp.status_code == 200 and "starting_cash_balance" in resp.json()


def test_the_daily_report_compares_with_the_day_before_as_close_does(client, shop, notes):
    _older(shop, total_sales=40)
    report = _submitted(client, shop, notes)

    sales = _get(client, shop, report.id).json()["sales"]

    assert sales["yesterday_total"] == 40
    assert len(sales["history"]) == 30 and sales["history"][-1] == {"date": D3.isoformat(), "total": 0}
    assert sales["history"][-2] == {"date": (D3 - timedelta(days=1)).isoformat(), "total": 40}


def test_an_older_report_can_be_read_but_not_edited_or_deleted(client, shop, notes):
    older = _older(shop)
    _submitted(client, shop, notes)

    body = _get(client, shop, older.id).json()

    assert body["is_latest"] is False
    assert body["permissions"]["edit"] is False and body["permissions"]["delete"] is False


def test_a_report_in_xero_says_so(client, shop, notes):
    report = _submitted(client, shop, notes)
    _set(report, xero_integrated=True, publishing_status="completed", status="published")

    body = _get(client, shop, report.id).json()

    assert (body["status_label"], body["status_tone"]) == ("Published to Xero", "success")
    assert body["was_published"] is True


@pytest.mark.parametrize("publishing, label, tone", [
    ("failed", "Publish failed", "danger"), ("publishing", "Publishing to Xero", "info"),
])
def test_a_publish_under_way_or_failed_says_so(client, shop, notes, publishing, label, tone):
    report = _submitted(client, shop, notes)
    _set(report, publishing_status=publishing)

    body = _get(client, shop, report.id).json()

    assert (body["status_label"], body["status_tone"]) == (label, tone) and body["was_published"] is True


def test_a_method_switched_off_since_still_shows_its_amount(client, shop, notes):
    report = _submitted(client, shop, notes)
    old = SaleInfo.objects.create(sale_name="Old Wallet", value_name="oldwallet_sales", type=SaleType.ELECTRONIC)
    ReportSale.objects.create(report_id=report.id, sale_id=old.id, amount=12)

    methods = _get(client, shop, report.id).json()["sales"]["methods"]

    assert [(m["name"], m["amount"]) for m in methods if m["value_name"] == "oldwallet_sales"] == [("Old Wallet", 12)]


def test_another_companys_or_an_unknown_report_is_404(client, shop, notes):
    theirs = make_report(make_entity(shop.owner, name="Other Shop"), D3, created_by=shop.owner.id)

    for report_id in (uuid.uuid4(), "nope", theirs.id):
        resp = _get(client, shop, report_id)
        assert resp.status_code == 404 and resp.json() == {"error": "Hmm, I couldn't find that report."}
    assert _post(client, shop, uuid.uuid4(), "/reopen").status_code == 404
    assert _delete(client, shop, uuid.uuid4()).status_code == 404
    assert _post(client, shop, theirs.id, "/share-link").status_code == 404


# --- reopen ------------------------------------------------------------------------------------------


def test_reopening_makes_the_latest_report_a_draft_from_opening(client, shop, notes):
    report = _submitted(client, shop, notes)
    assert _stock(shop) == {50: 50, 5: 5}

    resp = _post(client, shop, report.id, "/reopen")

    assert resp.status_code == 200
    assert resp.json() == {"report_id": str(report.id), "transaction_date": D3.isoformat(), "status": "draft",
                           "next": "opening"}
    row = Report.objects.get(id=report.id)
    assert row.status == "draft" and row.current_section == "opening" and row.completed_sections == []
    assert row.closing_balance == 55  # the figures stay
    assert _stock(shop) == {}  # no submitted report left: the stock falls back


def test_a_report_in_xero_reopens_and_keeps_its_xero_markers(client, shop, notes):
    report = _submitted(client, shop, notes)
    _set(report, xero_integrated=True, publishing_status="completed")

    assert _post(client, shop, report.id, "/reopen").status_code == 200

    row = Report.objects.get(id=report.id)
    assert row.xero_integrated is True and row.publishing_status == "completed"


@pytest.mark.parametrize("setup, status, message", [
    ({"status": "draft"}, 409, "This report is already a draft. Carry on with the Daily Closing."),
    ({"publishing_status": "publishing"}, 409,
     "Xero is still publishing this report. Wait until it finishes, then edit it."),
])
def test_fixed_reopening_is_refused_for_a_draft_or_while_xero_publishes(client, shop, notes, setup, status, message):
    report = _submitted(client, shop, notes)
    _set(report, **setup)

    resp = _post(client, shop, report.id, "/reopen")

    assert resp.status_code == status and resp.json() == {"error": message}


def test_only_the_latest_report_can_be_reopened(client, shop, notes):
    older = _older(shop)
    _submitted(client, shop, notes)

    resp = _post(client, shop, older.id, "/reopen")

    assert resp.status_code == 400
    assert resp.json()["error"] == "You can only edit the most recent report. Delete or edit the newer reports first."
    assert Report.objects.get(id=older.id).status == "submitted"


def test_a_cashier_may_not_delete_someone_elses_report_and_a_base_member_cannot_see_it(client, shop, notes):
    report = _submitted(client, shop, notes)
    cashier, base = make_user("cashier@test.com"), make_user("base@test.com")
    UserEntity.objects.create(user_id=cashier.id, entity_id=shop.entity.id, role=EntityRole.CASHIER, approved=True)
    UserEntity.objects.create(user_id=base.id, entity_id=shop.entity.id, role=EntityRole.ENTITY_BASE, approved=True)

    assert _get(client, shop, report.id, user=cashier).json()["permissions"]["delete"] is False
    refused = _delete(client, shop, report.id, user=cashier)
    assert refused.status_code == 403 and refused.json() == {"error": "You are not authorized to delete this report."}
    assert Report.objects.filter(id=report.id).exists()
    assert _get(client, shop, report.id, user=base).status_code == 404


# --- delete ------------------------------------------------------------------------------------------


@pytest.fixture
def deleted_files(monkeypatch):
    gone = []
    monkeypatch.setattr("petty_cash.files.storage.delete_files", lambda keys: gone.extend(keys))
    return gone


def test_deleting_the_latest_report_takes_everything_with_it(client, shop, notes, deleted_files,
                                                             django_capture_on_commit_callbacks):
    older = _older(shop, next_transaction_date=D3)
    report = _submitted(client, shop, notes)
    line = ReportExpense.objects.get(report_id=report.id)
    receipt = Attachment.objects.create(file_path="expenses/x/RECEIPT_15.jpg", original_name="r.jpg",
                                        mime_type="image/jpeg")
    ReportExpenseAttachment.objects.create(report_expense_id=line.id, attachment_id=receipt.id, sort_order=0,
                                           xero_attachment_id="")
    XeroReportSync.objects.create(report_id=report.id, xero_response_text="{}")

    with django_capture_on_commit_callbacks(execute=True):
        resp = _delete(client, shop, report.id)

    assert resp.status_code == 200
    assert resp.json() == {"deleted": str(report.id), "transaction_date": D3.isoformat(), "next": "dashboard"}
    assert not Report.objects.filter(id=report.id).exists()
    for model, column in ((ReportExpense, "report_id"), (ReportCashCount, "report_id"), (ReportSale, "report_id"),
                          (ReportHistory, "report_id"), (XeroReportSync, "report_id")):
        assert not model.objects.filter(**{column: report.id}).exists(), model.__name__
    assert not Attachment.objects.filter(id=receipt.id).exists()
    assert deleted_files == ["expenses/x/RECEIPT_15.jpg"]
    assert Report.objects.get(id=older.id).next_transaction_date is None
    assert _stock(shop) == {}  # the older report counted nothing


def test_fixed_a_failed_delete_keeps_the_report_and_its_receipts(client, shop, notes, deleted_files, monkeypatch,
                                                                 django_capture_on_commit_callbacks):
    report = _submitted(client, shop, notes)

    def fail(*_args):
        raise RuntimeError("the stock could not be refreshed")

    monkeypatch.setattr("petty_cash.services.report_actions.refresh_cash_stock", fail)
    with django_capture_on_commit_callbacks(execute=True):
        resp = _delete(client, shop, report.id)

    assert resp.status_code == 500
    assert Report.objects.filter(id=report.id).exists() and ReportExpense.objects.filter(report_id=report.id).exists()
    assert deleted_files == []


def test_deleting_is_refused_for_an_older_report_or_while_xero_publishes(client, shop, notes):
    older = _older(shop)
    latest = _submitted(client, shop, notes)

    resp = _delete(client, shop, older.id)
    assert resp.status_code == 400
    assert resp.json() == {"error": "I can only delete the most recent report, and there's a newer one after this."}

    _set(latest, publishing_status="publishing")
    resp = _delete(client, shop, latest.id)
    assert resp.status_code == 409
    assert resp.json() == {"error": "Xero is still publishing this report. Wait until it finishes before deleting it."}
    assert Report.objects.filter(id=latest.id).exists()


def test_rule_a_report_in_xero_can_be_deleted(client, shop, notes):
    """The owner's rule (2026-10-15): allowed; the app warns with was_published."""
    report = _submitted(client, shop, notes)
    _set(report, xero_integrated=True, publishing_status="completed", status="published")

    assert _delete(client, shop, report.id).status_code == 200


# --- share link --------------------------------------------------------------------------------------


def _verify(token: str) -> dict | None:
    """Flask's ``utils.verify_share_token``, as its public page checks the link."""
    import json
    import time

    params, expiry, signature = base64.b64decode(token).decode().split("|")
    expected = hmac.new(settings.SECRET_KEY.encode(), f"{params}|{expiry}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected) or time.time() > int(expiry):
        return None
    return json.loads(params)


def test_a_submitted_day_gets_a_share_link_flask_will_open(client, shop, notes):
    report = _submitted(client, shop, notes)

    resp = _post(client, shop, report.id, "/share-link")

    assert resp.status_code == 200, resp.json()
    link = ShareLink.objects.get(entity_id=shop.entity.id, transaction_date=D3)
    initials, day, secret = link.path_segment.split("/")
    assert (initials, day, len(secret)) == ("AS", D3.strftime("%d_%b_%Y"), 32)
    assert resp.json() == {"url": f"{settings.PETTY_CASH_WEB_URL}/share/{link.path_segment}",
                           "path": link.path_segment, "expires_at": link.expires_at.isoformat()}
    assert _verify(link.token) == {"entity_id": str(shop.entity.id), "transaction_date": D3.isoformat()}
    assert link.expires_at - Report.objects.get(id=report.id).submitted_at > timedelta(days=29)


def test_asking_again_renews_the_same_link(client, shop, notes):
    report = _submitted(client, shop, notes)

    first = _post(client, shop, report.id, "/share-link").json()
    again = _post(client, shop, report.id, "/share-link").json()

    assert (again["url"], again["path"]) == (first["url"], first["path"])  # the same link, renewed
    assert again["expires_at"] >= first["expires_at"]
    assert ShareLink.objects.filter(entity_id=shop.entity.id).count() == 1


def test_a_draft_is_not_shared(client, shop, notes):
    report = _submitted(client, shop, notes)
    _set(report, status="draft")

    resp = _post(client, shop, report.id, "/share-link")

    assert resp.status_code == 409 and resp.json() == {"error": "A draft can't be shared. Submit the day first."}


def test_a_name_without_latin_letters_is_prefixed_report(client, shop, notes):
    from shared_models.models import Entity

    Entity.objects.filter(id=shop.entity.id).update(name="小店")
    report = _submitted(client, shop, notes)

    _post(client, shop, report.id, "/share-link")

    assert ShareLink.objects.get(entity_id=shop.entity.id).path_segment.startswith("Report/")
