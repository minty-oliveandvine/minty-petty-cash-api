"""The Opening step (``petty_cash/routes/opening.py``) - Flask's
``tests/test_petty_cash_api_opening.py`` expectations, restated."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from petty_cash.days import today
from petty_cash.tests.conftest import headers, make_report, make_user, url
from shared_models.enums import EntityRole
from shared_models.models import EntityPettycashSettings, Report, ReportHistory, UserEntity

pytestmark = pytest.mark.django_db
TODAY = today()
D1, D2, D3 = TODAY - timedelta(days=3), TODAY - timedelta(days=2), TODAY - timedelta(days=1)


def _get(client, shop, day, user=None, raw=None):
    return client.get(url(shop.entity.id, "/reports/opening"),
                      {"transaction_date": raw or day.isoformat()}, headers=headers((user or shop.owner).id))


def _put(client, shop, body, user=None):
    return client.put(url(shop.entity.id, "/reports/opening"), body, content_type="application/json",
                      headers=headers((user or shop.owner).id))


def _body(day, **over):
    return {"transaction_date": day.isoformat(), "cash_addition": 0, "withdrawal": "personal", "action": "next", **over}


def _row(shop, day):
    return Report.objects.filter(entity_id=shop.entity.id, transaction_date=day).first()


# --- GET ---------------------------------------------------------------------------------------------


def test_a_first_report_reads_as_nothing_yet(client, shop):
    resp = _get(client, shop, D3)

    assert resp.status_code == 200, resp.content
    assert resp.json() == {
        "transaction_date": D3.isoformat(),
        "currency_code": "HKD",
        "draft": None,
        "starting_cash_balance": 0.0,
        "yesterday_closing_balance": 0.0,
        "safebox_balance": 0.0,
        "cash_addition": None,
        "withdrawal": "personal",
    }


def test_the_next_day_starts_from_the_last_closing(client, shop):
    make_report(shop.entity, D2, created_by=shop.owner.id, closing_balance=120, safe_box_balance=20)

    page = _get(client, shop, D3).json()

    assert page["starting_cash_balance"] == 120.0
    assert page["yesterday_closing_balance"] == 120.0 and page["safebox_balance"] == 20.0


def test_the_days_draft_brings_its_values(client, shop):
    make_report(shop.entity, D2, created_by=shop.owner.id, closing_balance=120)
    draft = make_report(shop.entity, D3, status="draft", created_by=shop.owner.id, opening_balance=120,
                        cash_addition=30, cash_addition_type="company", completed_sections=["opening"],
                        current_section="sales")

    page = _get(client, shop, D3).json()

    assert page["draft"] == {
        "id": draft.id, "status": "draft", "current_section": "sales", "completed_sections": ["opening"],
        "xero_integrated": False, "publishing_status": "unpublished",
    }
    assert page["cash_addition"] == 30.0 and page["withdrawal"] == "company"


def test_an_onboarding_seeded_opening_shows_when_there_is_no_yesterday(client, shop):
    make_report(shop.entity, D3, status="draft", opening_balance=500)

    assert _get(client, shop, D3).json()["starting_cash_balance"] == 500.0


def test_a_bad_date_and_a_member_without_the_right_are_refused(client, shop):
    assert _get(client, shop, D3, raw="2026-02-30").status_code == 422
    base = make_user("base@test.com")
    UserEntity.objects.create(user_id=base.id, entity_id=shop.entity.id, role=EntityRole.ENTITY_BASE, approved=True)

    assert _get(client, shop, D3, user=base).status_code == 403
    assert _put(client, shop, _body(D3), user=base).status_code == 403


# --- PUT ---------------------------------------------------------------------------------------------


def test_save_next_creates_the_draft_from_the_servers_opening(client, shop):
    make_report(shop.entity, D2, created_by=shop.owner.id, closing_balance=120)

    resp = _put(client, shop, _body(D3, cash_addition=30.5, withdrawal="company", opening_balance=99999))

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["next"] == "sales" and body["current_section"] == "sales" and body["completed_sections"] == ["opening"]
    row = _row(shop, D3)
    # the opening is the server's own (yesterday's 120) - a client's opening_balance is ignored
    assert (row.opening_balance, row.cash_addition, row.adjusted_opening_balance) == (120, Decimal("30.5"), Decimal("150.5"))
    assert row.closing_balance == Decimal("150.5")
    assert row.status == "draft" and row.cash_addition_type == "company" and row.created_by == shop.owner.id
    # Flask's Python-side defaults, written (rule 0)
    assert (row.bank_deposit, row.cashsale_total, row.expense_total, row.xero_integrated) == (0, 0, 0, False)
    assert row.next_transaction_date == D3 + timedelta(days=1)
    history = list(ReportHistory.objects.filter(report_id=row.id).values_list("action", "field_changed", "new_value"))
    assert history == [("created", "opening_entry", "Opening: 120.0, Addition: 30.5, Adjusted: 150.5")]


def test_save_exit_keeps_the_draft_on_opening(client, shop):
    body = _put(client, shop, _body(D3, action="exit")).json()

    assert body["next"] == "dashboard" and body["current_section"] == "opening"
    assert body["completed_sections"] == ["opening"]


def test_saving_again_updates_the_same_draft_without_touching_its_closing(client, shop):
    first = _put(client, shop, _body(D3, cash_addition=10)).json()
    Report.objects.filter(id=first["draft_id"]).update(closing_balance=999, current_section="expenses",
                                                      completed_sections=["opening", "sales"])

    second = _put(client, shop, _body(D3, cash_addition=25)).json()

    assert second["draft_id"] == first["draft_id"]
    row = _row(shop, D3)
    assert row.cash_addition == 25 and row.adjusted_opening_balance == 25
    assert row.closing_balance == 999  # not recomputed by this step
    assert second["current_section"] == "sales"  # Save & Next moves an advanced draft back
    assert second["completed_sections"] == ["opening", "sales"]
    assert [h.action for h in ReportHistory.objects.filter(report_id=row.id).order_by("created_at")] == ["created", "updated"]


def test_a_day_out_of_sequence_is_a_409_for_the_dashboard(client, shop):
    make_report(shop.entity, D1, created_by=shop.owner.id, closing_balance=1)

    resp = _put(client, shop, _body(D3))

    assert resp.status_code == 409
    assert resp.json() == {
        "error": f"Reports go one day at a time - this one needs to be {D2}, the day after your last submitted report ({D1}).",
        "to": "dashboard",
    }
    assert _row(shop, D3) is None


def test_a_future_day_is_refused(client, shop):
    resp = _put(client, shop, _body(TODAY + timedelta(days=1)))

    assert resp.status_code == 409
    assert resp.json()["error"] == (
        f"Transaction date cannot be in the future. You can only create reports for dates up to {TODAY}."
    )


def test_a_negative_opening_is_refused_back_to_the_page(client, shop):
    make_report(shop.entity, D2, created_by=shop.owner.id, closing_balance=-50)

    resp = _put(client, shop, _body(D3, cash_addition=10))

    assert resp.status_code == 409
    assert resp.json() == {"error": "Hmm, it looks like your cash balance is negative. Could you fix that first?", "to": "opening"}
    assert _row(shop, D3) is None


def test_no_report_starts_before_the_company_has_a_currency(client, shop):
    """Owner 2026-10-21: never a silent "$" - a company with no currency picks one first."""
    shop.entity.currency_id = None
    shop.entity.save(update_fields=["currency_id"])

    resp = _put(client, shop, _body(D3))

    assert resp.status_code == 409
    assert resp.json() == {
        "error": "No currency is selected for this company. Choose one in Petty Cash Settings to start a report.",
        "to": "dashboard",
    }
    assert _row(shop, D3) is None


def test_a_submitted_day_cannot_be_opened_again(client, shop):
    """The date rule answers first: after a submitted D3 only the next day may open."""
    make_report(shop.entity, D3, created_by=shop.owner.id, closing_balance=5)

    resp = _put(client, shop, _body(D3))

    assert resp.status_code == 409 and resp.json()["to"] == "dashboard"


@pytest.mark.parametrize(
    "over",
    [
        {"transaction_date": "14/10/2026"},
        {"cash_addition": -1},
        {"cash_addition": "10"},
        {"cash_addition": True},
        {"cash_addition": 100_000_000_000},
        {"withdrawal": "friend"},
        {"action": "save"},
    ],
)
def test_bad_input_is_a_422(client, shop, over):
    resp = _put(client, shop, _body(D3, **over))

    assert resp.status_code == 422 and resp.json()["error"]
    assert _row(shop, D3) is None


def test_a_body_that_is_not_json_is_a_422(client, shop):
    resp = client.put(url(shop.entity.id, "/reports/opening"), "not json", content_type="application/json",
                      headers=headers(shop.owner.id))

    assert resp.status_code == 422 and resp.json() == {"error": "Send the opening as JSON."}


# --- a company's first report (onboarding) ----------------------------------------------------------


def _onboarded(shop, *, opening=None, start=None):
    EntityPettycashSettings.objects.create(entity_id=shop.entity.id, opening_balance=opening, start_date=start)


def test_a_first_report_opens_and_is_saved_at_the_onboarding_balance(client, shop):
    _onboarded(shop, opening=500, start=D2)

    assert _get(client, shop, D3).json()["starting_cash_balance"] == 500.0
    assert _put(client, shop, _body(D3, cash_addition=20)).status_code == 200
    row = _row(shop, D3)
    assert (row.opening_balance, row.adjusted_opening_balance) == (500, 520)


def test_a_first_report_before_the_onboarding_start_is_a_409(client, shop):
    _onboarded(shop, start=D2)

    resp = _put(client, shop, _body(D1))

    assert resp.status_code == 409
    assert resp.json() == {"error": f"Transaction date must be on or after your onboarding date ({D2}).", "to": "dashboard"}


# --- no date: the latest draft of the last 7 days stands in ------------------------------------------


def _get_undated(client, shop):
    return client.get(url(shop.entity.id, "/reports/opening"), headers=headers(shop.owner.id))


def test_with_no_date_the_recent_draft_is_opened(client, shop):
    make_report(shop.entity, D2, created_by=shop.owner.id, closing_balance=120)
    draft = make_report(shop.entity, D3, status="draft", opening_balance=120, completed_sections=["opening"])

    page = _get_undated(client, shop).json()

    assert page["transaction_date"] == D3.isoformat() and page["draft"]["id"] == str(draft.id)


def test_with_no_date_a_stale_draft_is_passed_over_for_the_next_day(client, shop):
    make_report(shop.entity, D2, created_by=shop.owner.id, closing_balance=120)
    make_report(shop.entity, D1, status="draft", opening_balance=5)  # before the last submitted day

    page = _get_undated(client, shop).json()

    assert page["draft"] is None
    assert page["transaction_date"] == D3.isoformat() and page["starting_cash_balance"] == 120.0
