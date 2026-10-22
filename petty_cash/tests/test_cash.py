"""The Bank Deposit and Cash Count steps (``petty_cash/routes/cash.py``) - Flask's
``tests/test_petty_cash_api_cash.py`` and ``tests/test_report_deposit_cash_count_state.py``
expectations, restated. The owner's rules of 2026-10-15 are each a deliberate change from Flask's
old pages: a deposit above the cash on hand is refused, any cent is a difference, and Save & Next
needs a reason for one."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from petty_cash.days import today
from petty_cash.services.denominations import denominations_for
from petty_cash.tests.conftest import FACES, headers, make_report, make_user, url
from shared_models.enums import CashType, EntityRole
from shared_models.models import (
    CashInfo,
    EntityCashSetting,
    Report,
    ReportCashCount,
    ReportHistory,
    UserEntity,
)

pytestmark = pytest.mark.django_db
D3 = today() - timedelta(days=1)


def _draft(shop, **fields):
    """Cash on hand 155 (100 + 20 + 50 - 15)."""
    values = {"opening_balance": 100, "cash_addition": 20, "adjusted_opening_balance": 120, "cashsale_total": 50,
              "expense_total": 15, "closing_balance": 155, "completed_sections": ["opening", "sales", "expenses"],
              "current_section": "deposit"}
    values.update(fields)
    return make_report(shop.entity, D3, status=values.pop("status", "draft"), created_by=shop.owner.id, **values)


def _counted(shop, **fields):
    """Deposit 100 made: expected cash 55."""
    values = {"bank_deposit": 100, "closing_balance": 55, "current_section": "cash_count",
              "completed_sections": ["opening", "sales", "expenses", "deposit"]}
    values.update(fields)
    return _draft(shop, **values)


def _row(report) -> Report:
    return Report.objects.get(id=report.id)


def _counts(report) -> dict:
    return {float(v): q for v, q in ReportCashCount.objects.filter(report_id=report.id)
            .values_list("cash_value", "quantity") if q}


def _history(report) -> list:
    return list(ReportHistory.objects.filter(report_id=report.id).order_by("created_at")
                .values_list("field_changed", "new_value"))


def _get(client, shop, step, day=D3, user=None):
    query = {"transaction_date": day.isoformat()} if day else {}
    return client.get(url(shop.entity.id, f"/reports/{step}"), query, headers=headers((user or shop.owner).id))


def _put(client, shop, step, body, user=None):
    return client.put(url(shop.entity.id, f"/reports/{step}"),
                      {"transaction_date": D3.isoformat(), "action": "next", **body},
                      content_type="application/json", headers=headers((user or shop.owner).id))


# --- Deposit -----------------------------------------------------------------------------------------


def test_the_deposit_read_brings_the_cash_on_hand_and_what_it_is_made_of(client, shop):
    draft = _draft(shop, bank_deposit=30)

    resp = _get(client, shop, "deposit")

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["draft"]["id"] == str(draft.id) and body["currency_code"] == "HKD"
    assert {k: body[k] for k in ("opening_balance", "cash_addition", "cash_sales", "expenses", "bank_deposit",
                                 "cash_on_hand", "expected_cash")} == {
        "opening_balance": 100, "cash_addition": 20, "cash_sales": 50, "expenses": 15, "bank_deposit": 30,
        "cash_on_hand": 155, "expected_cash": 125}


def test_the_reads_refuse_no_draft_a_bad_date_and_a_member_without_the_right(client, shop, notes):
    for step in ("deposit", "cash-count"):
        resp = _get(client, shop, step)
        assert resp.status_code == 409 and resp.json()["to"] == "opening"
        assert _get(client, shop, step, day=None).status_code == 422
    base = make_user("base@test.com")
    UserEntity.objects.create(user_id=base.id, entity_id=shop.entity.id, role=EntityRole.ENTITY_BASE, approved=True)
    _draft(shop)
    assert _get(client, shop, "deposit", user=base).status_code == 403
    assert _put(client, shop, "deposit", {"bank_deposit": 1}, user=base).status_code == 403
    assert _put(client, shop, "cash-count", {"counts": {}}, user=base).status_code == 403


def test_save_next_stores_the_deposit_and_moves_on(client, shop):
    draft = _draft(shop)

    resp = _put(client, shop, "deposit", {"bank_deposit": 100.5})

    assert resp.status_code == 200, resp.json()
    assert resp.json()["next"] == "cash_count" and resp.json()["current_section"] == "cash_count"
    row = _row(draft)
    assert row.bank_deposit == Decimal("100.50") and row.closing_balance == Decimal("54.50")
    assert row.completed_sections == ["opening", "sales", "expenses", "deposit"]
    assert _history(draft) == [("bank_deposit", "Bank Deposit: $100.5")]


def test_save_exit_stores_the_deposit_without_moving_on(client, shop):
    draft = _draft(shop)

    body = _put(client, shop, "deposit", {"bank_deposit": 10, "action": "exit"}).json()

    assert body["next"] == "dashboard"
    row = _row(draft)
    assert row.bank_deposit == 10 and row.completed_sections == ["opening", "sales", "expenses"]


def test_the_deposit_makes_the_saver_the_reports_creator(client, shop):
    clerk = make_user("clerk@test.com")
    UserEntity.objects.create(user_id=clerk.id, entity_id=shop.entity.id, role=EntityRole.ADMIN, approved=True)
    draft = _draft(shop)

    _put(client, shop, "deposit", {"bank_deposit": 1}, user=clerk)

    assert _row(draft).created_by == clerk.id


def test_rule_a_deposit_above_the_cash_on_hand_is_a_422_on_the_field(client, shop):
    """The owner's rule, 2026-10-15 - Flask's page accepted it and blocked only at Cash Count."""
    draft = _draft(shop)

    resp = _put(client, shop, "deposit", {"bank_deposit": 155.01})

    assert resp.status_code == 422
    assert resp.json() == {"error": "The deposit can't be more than the cash on hand. Enter HKD\u00a0155.00 or less.",
                           "field": "bank_deposit"}
    assert _row(draft).bank_deposit == 0 and _history(draft) == []


def test_a_negative_cash_on_hand_is_a_409(client, shop):
    _draft(shop, expense_total=500)

    resp = _put(client, shop, "deposit", {"bank_deposit": 0})

    assert resp.status_code == 409
    assert resp.json() == {"error": "Hmm, it looks like your cash balance is negative. Could you fix that first?"}


def test_a_report_no_longer_a_draft_is_not_the_days_draft(client, shop):
    _draft(shop, status="submitted")

    resp = _put(client, shop, "deposit", {"bank_deposit": 1})

    assert resp.status_code == 409 and resp.json()["to"] == "opening"


@pytest.mark.parametrize("body", [
    {"bank_deposit": -1}, {"bank_deposit": "12"}, {"bank_deposit": True}, {"bank_deposit": 100_000_000_000},
    {"bank_deposit": 1, "action": "later"}, {"bank_deposit": 1, "transaction_date": "15/10/2026"},
])
def test_bad_deposit_input_is_refused(client, shop, body):
    draft = _draft(shop)

    assert _put(client, shop, "deposit", body).status_code == 422
    assert _row(draft).bank_deposit == 0


def test_a_deposit_that_is_not_json_is_refused(client, shop):
    _draft(shop)

    resp = client.put(url(shop.entity.id, "/reports/deposit"), "nope", content_type="application/json",
                      headers=headers(shop.owner.id))

    assert resp.status_code == 422 and resp.json() == {"error": "Send the deposit as JSON."}


# --- Cash Count --------------------------------------------------------------------------------------


def test_the_count_read_brings_the_denominations_the_expected_cash_and_the_saved_count(client, shop, notes):
    _counted(shop, safe_box_balance=5, discrepancy_amount=-10, discrepancy_type="short", discrepancy_reason="Change")
    _put(client, shop, "cash-count",
         {"counts": {notes[20]: 2}, "safe_box_balance": 5, "discrepancy_reason": "Change", "action": "exit"})

    body = _get(client, shop, "cash-count").json()

    assert [d["value"] for d in body["denominations"]] == list(FACES)
    assert body["denominations"][0] == {"id": notes[1000], "value": 1000, "kind": "note", "name": "1000",
                                          "count": 0}
    assert {d["value"]: d["count"] for d in body["denominations"] if d["count"]} == {20: 2}
    assert body["expected_cash"] == 55 and body["safe_box_balance"] == 5
    assert body["discrepancy"] == {"amount": -10, "kind": "short", "reason": "Change"}


def test_an_exact_count_moves_on_to_ending(client, shop, notes):
    draft = _counted(shop)

    resp = _put(client, shop, "cash-count", {"counts": {notes[50]: 1, notes[5]: 1}})

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["next"] == "ending" and body["discrepancy"] == {"amount": 0, "kind": "none"}
    assert _counts(draft) == {50: 1, 5: 1}
    row = _row(draft)
    assert row.completed_sections == ["opening", "sales", "expenses", "deposit", "cash_count"]
    assert row.current_section == "ending"
    assert row.closing_balance == 55  # the count never writes it; submit adds the difference
    assert _history(draft) == [("cash_count", "Cash Count: $55.0")]


def test_a_short_count_is_negative_and_the_safebox_counts_towards_it(client, shop, notes):
    draft = _counted(shop)

    _put(client, shop, "cash-count",
         {"counts": {notes[20]: 2}, "safe_box_balance": 5, "discrepancy_reason": "Gave change twice"})

    row = _row(draft)
    assert (row.discrepancy_amount, row.discrepancy_type) == (Decimal("-10.00"), "short")
    assert row.safe_box_balance == 5 and row.discrepancy_reason == "Gave change twice"


def test_an_over_count_is_positive(client, shop, notes):
    draft = _counted(shop)

    _put(client, shop, "cash-count", {"counts": {notes[50]: 1, notes[10]: 1}, "discrepancy_reason": "Tip"})

    row = _row(draft)
    assert (row.discrepancy_amount, row.discrepancy_type) == (Decimal("5.00"), "over")


def test_rule_a_difference_needs_a_reason_on_save_next_but_not_on_save_exit(client, shop, notes):
    """The owner's rule, 2026-10-15 - Flask asked only in the browser."""
    draft = _counted(shop)

    refused = _put(client, shop, "cash-count", {"counts": {notes[50]: 1}})
    assert refused.status_code == 422
    assert refused.json() == {"error": "The count is HKD\u00a05.00 short. Add the reason to continue.",
                              "field": "discrepancy_reason"}
    assert _counts(draft) == {} and _history(draft) == []

    kept = _put(client, shop, "cash-count", {"counts": {notes[50]: 1}, "action": "exit"})
    assert kept.status_code == 200 and kept.json()["discrepancy"] == {"amount": -5, "kind": "short"}
    assert _row(draft).current_section == "cash_count"

    done = _put(client, shop, "cash-count", {"counts": {notes[50]: 1}, "discrepancy_reason": " Float "})
    assert done.status_code == 200 and _row(draft).discrepancy_reason == "Float"


def test_rule_a_single_cent_is_a_difference(client, shop, notes):
    """The owner's rule, 2026-10-15 - Flask's check was "more than 0.01"."""
    draft = _counted(shop, expense_total=Decimal("14.99"))  # expected 55.01

    resp = _put(client, shop, "cash-count", {"counts": {notes[50]: 1, notes[5]: 1}})
    assert resp.status_code == 422
    assert resp.json()["error"] == "The count is HKD\u00a00.01 short. Add the reason to continue."

    _put(client, shop, "cash-count", {"counts": {notes[50]: 1, notes[5]: 1}, "discrepancy_reason": "Rounding"})
    row = _row(draft)
    assert (row.discrepancy_amount, row.discrepancy_type) == (Decimal("-0.01"), "short")


def test_save_exit_keeps_the_count_and_a_recount_replaces_it(client, shop, notes):
    draft = _counted(shop)

    _put(client, shop, "cash-count", {"counts": {notes[50]: 1, notes[5]: 1}, "action": "exit"})
    assert _counts(draft) == {50: 1, 5: 1}
    assert _row(draft).completed_sections == ["opening", "sales", "expenses", "deposit"]

    _put(client, shop, "cash-count", {"counts": {notes[50]: 1, notes[5]: 0, notes[1]: 5}})
    assert _counts(draft) == {50: 1, 1: 5}


def test_a_negative_expected_cash_is_a_409(client, shop, notes):
    draft = _counted(shop, bank_deposit=500)  # 155 - 500 < 0

    resp = _put(client, shop, "cash-count", {"counts": {notes[50]: 1}})

    assert resp.status_code == 409
    assert resp.json() == {"error": "Hmm, it looks like your cash balance is negative. Could you fix that first?"}
    assert _counts(draft) == {}


def test_a_company_without_denominations_is_a_409(client, shop):
    _counted(shop)

    resp = _put(client, shop, "cash-count", {"counts": {}})

    assert resp.status_code == 409 and "denominations" in resp.json()["error"]


@pytest.mark.parametrize("change", [
    {"counts": {"not-a-denomination": 1}},
    {"counts": "fifty"},
    {"safe_box_balance": -1},
    {"discrepancy_reason": 7},
    {"discrepancy_reason": "x" * 301},
    {"bank_deposit": -1},
    {"action": "later"},
])
def test_bad_count_input_is_refused(client, shop, notes, change):
    draft = _counted(shop)

    assert _put(client, shop, "cash-count", {"counts": {}, **change}).status_code == 422
    row = _row(draft)
    assert row.current_section == "cash_count" and row.discrepancy_reason is None


@pytest.mark.parametrize("quantity", [2.5, -1, True, "3", 1_000_001])
def test_a_count_must_be_a_whole_number_of_zero_or_more(client, shop, notes, quantity):
    draft = _counted(shop)

    resp = _put(client, shop, "cash-count", {"counts": {notes[50]: quantity}})

    assert resp.status_code == 422 and resp.json()["field"] == notes[50]
    assert _counts(draft) == {}


# --- the company's own denominations -----------------------------------------------------------------


def test_the_companys_settings_switch_off_and_reorder_without_touching_the_catalogue(client, shop, notes):
    """Flask set the company's order onto the shared ``cash_info`` rows in memory; this port reads
    the override and leaves the catalogue alone."""
    EntityCashSetting.objects.create(entity_id=shop.entity.id, cash_id=notes[1000], is_active=False)
    EntityCashSetting.objects.create(entity_id=shop.entity.id, cash_id=notes[0.1], is_active=True, display_order=0)
    _counted(shop)

    values = [d["value"] for d in _get(client, shop, "cash-count").json()["denominations"]]

    assert values == [0.1, 500, 100, 50, 20, 10, 5, 2, 1, 0.5, 0.2]
    assert CashInfo.objects.get(id=notes[0.1]).display_order == 12
    assert _put(client, shop, "cash-count", {"counts": {notes[1000]: 1}}).status_code == 422


def test_another_currencys_denominations_are_not_the_companys(shop, notes):
    from petty_cash.tests.conftest import make_currency

    other = make_currency("SGD")
    CashInfo.objects.create(currency_id=other.id, cash_value=Decimal("2"), type=CashType.NOTE, display_order=1)

    assert [d.value for d in denominations_for(shop.entity.id)] == [Decimal(str(v)).quantize(Decimal("0.01"))
                                                                     for v in FACES]


# --- the deposit and the count together (Part 15's Cash Count stage) ---------------------------------


def test_the_deposit_and_the_count_are_saved_together(client, shop, notes):
    draft = _draft(shop)  # no deposit yet: cash on hand 155

    resp = _put(client, shop, "cash-count", {"bank_deposit": 100, "counts": {notes[50]: 1, notes[5]: 1}})

    assert resp.status_code == 200, resp.json()
    row = _row(draft)
    assert row.bank_deposit == 100 and row.discrepancy_type == "none"
    assert row.completed_sections == ["opening", "sales", "expenses", "deposit", "cash_count"]
    assert row.current_section == "ending"
    assert _history(draft) == [("bank_deposit", "Bank Deposit: $100.0"), ("cash_count", "Cash Count: $55.0")]


def test_a_refused_count_leaves_the_deposit_unsaved_too(client, shop, notes):
    draft = _draft(shop)

    resp = _put(client, shop, "cash-count", {"bank_deposit": 100, "counts": {notes[50]: 1}})  # 5 short

    assert resp.status_code == 422 and resp.json()["field"] == "discrepancy_reason"
    row = _row(draft)
    assert row.bank_deposit == 0 and row.completed_sections == ["opening", "sales", "expenses"]
    assert _counts(draft) == {} and _history(draft) == []


def test_a_deposit_above_the_cash_on_hand_refuses_the_whole_save(client, shop, notes):
    draft = _draft(shop)

    resp = _put(client, shop, "cash-count", {"bank_deposit": 200, "counts": {notes[50]: 1}})

    assert resp.status_code == 422 and resp.json()["field"] == "bank_deposit"
    assert _counts(draft) == {}


# How a refusal writes an amount (owner 2026-10-21): the code, a NON-BREAKING space, two places;
# with no currency the number says so - "(No currency selected)" - never a "$".
@pytest.mark.parametrize(("code", "value", "shown"), [
    ("HKD", Decimal("1234.5"), "HKD\u00a01,234.50"),
    ("HKD", Decimal("0.005"), "HKD\u00a00.01"),
    (None, Decimal("155"), "155.00 (No currency selected)"),
    ("", Decimal("12345678901.99"), "12,345,678,901.99 (No currency selected)"),
])
def test_money_in_a_refusal(monkeypatch, code, value, shown):
    from petty_cash.services import cash

    monkeypatch.setattr(cash, "currency_code", lambda entity_id: code)
    assert cash._money(Report(entity_id=None), value) == shown
