"""The last step (``petty_cash/routes/close.py``): the day's summary, the submit and the outcome -
Flask's ``tests/test_petty_cash_api_close.py`` and ``tests/test_report_submit_state.py``
expectations, restated. FIXED / RULE tests pin what Flask's submit service changed on purpose
(2026-10-15): nothing is ever deleted, and a difference that no longer matches the count is refused
(the owner's rule)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from petty_cash.days import today
from petty_cash.tests.conftest import headers, make_entity, make_report, make_user, url
from shared_models.enums import CashType, EntityRole, PublishStatus
from shared_models.models import (
    CashInfo,
    EntityCashDetail,
    Report,
    ReportCashCount,
    ReportExpense,
    UserEntity,
)

pytestmark = pytest.mark.django_db
D3 = today() - timedelta(days=1)
ALL = ["opening", "sales", "expenses", "deposit", "cash_count"]


def _ready(shop, notes, *, counts=None, day=D3, item="Advertising", **fields):
    """A draft with every step done: opening 100 + 20 added + 50 cash sales - 15 expenses - 100
    deposit = 55 expected, counted as one 50 note and one 5 coin unless ``counts`` (face -> n)."""
    values = {"opening_balance": 100, "cash_addition": 20, "adjusted_opening_balance": 120, "cashsale_total": 50,
              "expense_total": 15, "bank_deposit": 100, "closing_balance": 55, "safe_box_balance": 0,
              "discrepancy_amount": 0, "discrepancy_type": "none", "completed_sections": list(ALL),
              "current_section": "ending"}
    values.update(fields)
    report = make_report(shop.entity, day, status="draft", created_by=shop.owner.id, **values)
    if item is not None:  # None: a day with no expense lines
        ReportExpense.objects.create(report_id=report.id, item=item, amount=15)
    for face, n in (counts or {50: 1, 5: 1}).items():
        ReportCashCount.objects.create(report_id=report.id, cash_id=notes[face], quantity=n,
                                       cash_value=Decimal(str(face)))
    return report


def _row(report) -> Report:
    return Report.objects.get(id=report.id)


def _stock(shop) -> dict:
    faces = dict(CashInfo.objects.values_list("id", "cash_value"))
    return {float(faces[c]): float(m) for c, m in EntityCashDetail.objects.filter(entity_id=shop.entity.id)
            .values_list("cash_id", "cash_instock") if m}


def _close(client, shop, day=D3, user=None):
    query = {"transaction_date": day.isoformat()} if day else {}
    return client.get(url(shop.entity.id, "/reports/close"), query, headers=headers((user or shop.owner).id))


def _submit(client, shop, day=D3, user=None, body=None):
    return client.post(url(shop.entity.id, "/reports/submit"), body or {"transaction_date": day.isoformat()},
                       content_type="application/json", headers=headers((user or shop.owner).id))


def _outcome(client, shop, report_id, user=None):
    return client.get(url(shop.entity.id, f"/reports/{report_id}/outcome"), headers=headers((user or shop.owner).id))


# --- the summary -------------------------------------------------------------------------------------


def test_the_close_read_brings_the_day_as_submit_will_post_it(client, shop, notes):
    report = _ready(shop, notes)

    resp = _close(client, shop)

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["draft"]["id"] == str(report.id) and body["currency_code"] == "HKD"
    assert (body["opening_balance"], body["cash_addition"]) == (100, 20)
    assert body["sales"]["cash"] == 50
    assert body["sales"]["yesterday_total"] is None  # no report the day before
    assert len(body["sales"]["history"]) == 30 and body["sales"]["history"][-1]["date"] == D3.isoformat()
    assert body["expenses"]["total"] == 15
    assert body["expenses"]["lines"][0] | {"id": None} == {"id": None, "item": "Advertising", "supplier": "",
                                                            "remarks": "", "amount": 15, "receipts": 0}
    assert body["cash"] == {"bank_deposit": 100, "expected": 55, "counted": 55, "safe_box": 0,
                            "difference": {"amount": 0, "kind": "none", "reason": ""}}
    assert body["closing_balance"] == 55
    assert body["problem"] is None


def test_the_sales_history_counts_the_days_before(client, shop, notes):
    for back, total in ((1, 400), (3, 250)):
        make_report(shop.entity, D3 - timedelta(days=back), created_by=shop.owner.id, total_sales=total,
                    opening_balance=0)
    _ready(shop, notes, total_sales=500)

    sales = _close(client, shop).json()["sales"]

    assert sales["yesterday_total"] == 400
    totals = {h["date"]: h["total"] for h in sales["history"]}
    assert totals[(D3 - timedelta(days=1)).isoformat()] == 400
    assert totals[(D3 - timedelta(days=3)).isoformat()] == 250
    assert totals[D3.isoformat()] == 500 and totals[(D3 - timedelta(days=2)).isoformat()] == 0


def test_the_close_read_says_what_would_stop_the_submit(client, shop, notes):
    _ready(shop, notes, bank_deposit=90)  # changed after the count

    body = _close(client, shop).json()

    assert body["problem"] == {"error": "The figures changed after the cash count. Check the cash count again, "
                                        "then submit.", "to": "cash_count"}


def test_no_draft_is_a_409_to_opening_and_a_submitted_day_names_its_report(client, shop, notes):
    resp = _close(client, shop)
    assert resp.status_code == 409 and resp.json()["to"] == "opening"

    report = _ready(shop, notes)
    _submit(client, shop)
    again = _close(client, shop)
    assert again.status_code == 409
    assert again.json() == {"error": "This day is already submitted.", "to": "submitted", "report_id": str(report.id)}


# --- the submit --------------------------------------------------------------------------------------


def test_submitting_posts_the_day(client, shop, notes):
    report = _ready(shop, notes)

    resp = _submit(client, shop)

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["report_id"] == str(report.id) and body["status"] == "submitted" and body["next"] == "submitted"
    assert body["closing_balance"] == 55 and body["submitted_at"]
    row = _row(report)
    assert row.status == "submitted" and row.submitted_at is not None
    assert row.current_section == "submitted"
    assert row.completed_sections == ALL + ["ending", "submitted"]
    assert _stock(shop) == {50: 50, 5: 5}  # the stock follows the submitted count


def test_a_short_count_closes_on_what_was_counted(client, shop, notes):
    report = _ready(shop, notes, counts={50: 1}, discrepancy_amount=-5, discrepancy_type="short",
                    discrepancy_reason="Change")

    _submit(client, shop)

    assert _row(report).closing_balance == 50  # 55 expected + (-5)


def test_the_submitter_becomes_the_reports_creator(client, shop, notes):
    clerk = make_user("clerk@test.com")
    UserEntity.objects.create(user_id=clerk.id, entity_id=shop.entity.id, role=EntityRole.ADMIN, approved=True)
    report = _ready(shop, notes)

    _submit(client, shop, user=clerk)

    assert _row(report).created_by == clerk.id


def test_a_report_resubmitted_after_xero_is_no_longer_marked_published(client, shop, notes):
    report = _ready(shop, notes, xero_integrated=True, publishing_status=PublishStatus.COMPLETED)

    _submit(client, shop)

    row = _row(report)
    assert row.xero_integrated is False and row.publishing_status == PublishStatus.COMPLETED


def test_the_stock_drops_other_currencies_rows(client, shop, notes):
    from petty_cash.tests.conftest import make_currency

    other = CashInfo.objects.create(currency_id=make_currency("SGD").id, cash_value=Decimal("2"),
                                    type=CashType.NOTE, display_order=1)
    EntityCashDetail.objects.create(entity_id=shop.entity.id, cash_id=other.id, cash_instock=Decimal("4"))
    _ready(shop, notes)

    _submit(client, shop)

    assert not EntityCashDetail.objects.filter(entity_id=shop.entity.id, cash_id=other.id).exists()
    assert EntityCashDetail.objects.filter(entity_id=shop.entity.id).count() == len(notes)


# --- refusals ----------------------------------------------------------------------------------------


def test_a_step_not_done_names_the_step_and_writes_nothing(client, shop, notes):
    report = _ready(shop, notes, completed_sections=["opening", "sales", "expenses", "deposit"])

    resp = _submit(client, shop)

    assert resp.status_code == 409
    assert resp.json() == {"error": "Finish Cash Count before you submit the day.", "to": "cash_count"}
    assert _row(report).status == "draft" and _stock(shop) == {}


def test_a_missing_deposit_is_fixed_in_cash_count(client, shop, notes):
    _ready(shop, notes, completed_sections=["opening", "sales", "expenses", "cash_count"])

    assert _submit(client, shop).json() == {"error": "Finish Cash Count before you submit the day.",
                                            "to": "cash_count"}


def test_a_second_submit_is_told_the_day_is_already_submitted(client, shop, notes):
    report = _ready(shop, notes)
    _submit(client, shop)

    resp = _submit(client, shop)

    assert resp.status_code == 409
    assert resp.json() == {"error": "This day is already submitted.", "to": "submitted", "report_id": str(report.id)}


def test_the_submit_refuses_a_bad_date_and_a_member_without_the_right(client, shop, notes):
    _ready(shop, notes)
    assert _submit(client, shop, body={"transaction_date": "15/10/2026"}).status_code == 422
    base = make_user("base@test.com")
    UserEntity.objects.create(user_id=base.id, entity_id=shop.entity.id, role=EntityRole.ENTITY_BASE, approved=True)
    assert _submit(client, shop, user=base).status_code == 403
    assert _close(client, shop, user=base).status_code == 403


def test_a_negative_expected_cash_is_refused(client, shop, notes):
    report = _ready(shop, notes, bank_deposit=500)

    resp = _submit(client, shop)

    assert resp.status_code == 409
    assert resp.json() == {"error": "Hmm, it looks like your cash balance is negative. Could you fix that first?",
                           "to": "cash_count"}
    assert _row(report).status == "draft"


def test_a_future_day_is_refused(client, shop, notes):
    tomorrow = today() + timedelta(days=1)
    report = _ready(shop, notes, day=tomorrow)

    resp = _submit(client, shop, day=tomorrow)

    assert resp.status_code == 409 and resp.json()["to"] == "dashboard"
    assert _row(report).status == "draft"


def test_fixed_an_expense_line_without_an_item_is_sent_to_expenses(client, shop, notes):
    report = _ready(shop, notes, item="")

    resp = _submit(client, shop)

    assert resp.json() == {"error": "An expense has no account or no amount. Fix it in Expenses, then submit.",
                           "to": "expenses"}
    assert _row(report).status == "draft"


def test_rule_a_difference_saved_before_a_later_deposit_change_is_refused(client, shop, notes):
    """RULE (owner, 2026-10-15): Flask kept the count's difference although the deposit changed after
    it, so the closing (and Xero) were wrong."""
    report = _ready(shop, notes, bank_deposit=90)  # expected is 65 now; 55 was counted

    resp = _submit(client, shop)

    assert resp.json() == {"error": "The figures changed after the cash count. Check the cash count again, "
                                    "then submit.", "to": "cash_count"}
    assert _row(report).status == "draft"


def test_rule_a_difference_without_its_reason_is_refused(client, shop, notes):
    report = _ready(shop, notes, counts={50: 1}, discrepancy_amount=-5, discrepancy_type="short")

    resp = _submit(client, shop)

    assert resp.json() == {"error": "The count has a difference with no reason. Add the reason in Cash Count.",
                           "to": "cash_count"}
    assert _row(report).status == "draft"


def test_fixed_a_failure_during_submit_leaves_the_draft_as_it_was(client, shop, notes, monkeypatch):
    """FIXED: Flask's clean-up deleted a day with no expense lines after any failure inside the submit."""
    report = _ready(shop, notes, item=None, expense_total=0, bank_deposit=115)  # expected 55

    def fail(*_args, **_kwargs):
        raise RuntimeError("the stock could not be refreshed")

    monkeypatch.setattr("petty_cash.services.submit.refresh_cash_stock", fail)
    resp = _submit(client, shop)

    assert resp.status_code == 500
    assert resp.json() == {"error": "Something went wrong on our side. Please try again."}
    row = _row(report)
    assert row.status == "draft" and row.closing_balance == 55 and row.completed_sections == ALL
    assert row.submitted_at is None


# --- the outcome -------------------------------------------------------------------------------------


def test_the_outcome_says_what_was_counted_and_what_happens_next(client, shop, notes):
    report = _ready(shop, notes, counts={50: 1}, discrepancy_amount=-5, discrepancy_type="short",
                    discrepancy_reason="Change", safe_box_balance=0)
    _submit(client, shop)

    resp = _outcome(client, shop, report.id)

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["report_id"] == str(report.id) and body["submitted"] is True and body["status"] == "submitted"
    assert body["cash_counted"] == 50
    assert body["difference"] == {"amount": -5, "kind": "short", "has_reason": True}
    assert body["xero"] == {"publishing_status": "unpublished", "published_at": None, "xero_integrated": False,
                            "was_published": False}
    assert body["next_day"] == (D3 + timedelta(days=1)).isoformat() and body["is_today"] is False
    assert body["company_name"] == "Acme Shop" and body["submitted_by"] == "Olive Owner"
    assert body["submitted_at"].endswith("+08:00")  # Hong Kong time, as Flask's outcome page
    assert body["can_publish"] is True


def test_the_outcomes_totals_are_the_daily_reports_kpis(client, shop, notes):
    report = _ready(shop, notes, total_sales=80)
    _submit(client, shop)

    body = _outcome(client, shop, report.id).json()
    kpis = client.get(url(shop.entity.id, f"/reports/{report.id}"), headers=headers(shop.owner.id)).json()["kpis"]

    assert (body["total_sales"], body["total_expenses"]) == (kpis["total_sales"], kpis["total_expenses"])
    assert (body["total_sales"], body["total_expenses"]) == (80, 15)


def test_the_outcome_of_another_companys_or_an_unknown_report_is_404(client, shop, notes):
    other = make_entity(shop.owner, name="Other Shop")
    theirs = make_report(other, D3, created_by=shop.owner.id)

    for report_id in (uuid.uuid4(), "not-a-uuid", theirs.id):
        resp = _outcome(client, shop, report_id)
        assert resp.status_code == 404 and resp.json() == {"error": "Hmm, I couldn't find that report."}
