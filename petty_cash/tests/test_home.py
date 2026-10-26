"""``GET /home`` (``petty_cash/routes/home.py``) - Flask's ``tests/test_petty_cash_api_home.py``
expectations, restated: what the dashboard computes and what this person may do there."""

from __future__ import annotations

from datetime import timedelta

import pytest

from petty_cash.days import today
from petty_cash.tests.conftest import headers, make_report, make_user, url
from shared_models.enums import EntityRole, SystemRole
from shared_models.models import EntityPettycashSettings, UserEntity

pytestmark = pytest.mark.django_db
TODAY = today()


def _home(client, shop, user=None):
    resp = client.get(url(shop.entity.id, "/home"), headers=headers((user or shop.owner).id))
    assert resp.status_code == 200, resp.content
    return resp.json()


def _member(shop, email, role=EntityRole.CASHIER, approved=True):
    person = make_user(email)
    UserEntity.objects.create(user_id=person.id, entity_id=shop.entity.id, role=role, approved=approved)
    return person


def test_a_new_company(client, shop):
    home = _home(client, shop)

    assert home["currency_code"] == "HKD"
    assert home["server_today"] == TODAY.isoformat()
    assert home["user_count"] == 1
    assert home["latest"] == {
        "date": None,
        "closing_balance": 0.0,
        "deposit": 0.0,
        "deposit_bank_name": "No deposit",
        "deposit_bank_number": "",
        "deposit_correction_missing_settings": [],
        "can_correct_deposit": True,
        "submitter_first_name": None,
    }
    assert home["draft"] is None and home["draft_date"] is None
    assert home["start_date"] is None
    assert home["published"] == {}
    assert home["today_has_report"] is False and home["today_is_latest_report"] is False
    assert home["xero_settings"] == {
        "complete": False,
        "missing": [
            "Main Bank Account", "Deposit Bank Account", "Cash Sale Account", "Cash Sale Contact",
            "Owners Account", "Owners Contact", "Discrepancy Account", "Discrepancy Bank Account",
            "Discrepancy Contact",
        ],
    }
    assert home["permissions"] == {"read_only": False, "correct_deposit": True}


def test_a_draft_after_submitted_days_reads_as_the_page_does(client, shop):
    yesterday = TODAY - timedelta(days=1)
    submitted = make_report(shop.entity, yesterday, created_by=shop.owner.id, closing_balance=120,
                            bank_deposit=50, xero_integrated=True)
    draft = make_report(shop.entity, TODAY, status="draft", created_by=shop.owner.id, closing_balance=150,
                        current_section="sales", completed_sections=["opening"])

    home = _home(client, shop)

    assert home["latest"]["date"] == TODAY.isoformat() and home["latest"]["closing_balance"] == 150.0
    assert home["latest"]["submitter_first_name"] == "Olive"
    assert home["draft"]["id"] == draft.id
    assert home["draft"]["current_section"] == "sales" and home["draft"]["completed_sections"] == ["opening"]
    assert 0 <= home["draft"]["last_edit_seconds"] < 600
    assert home["draft_date"] == TODAY.isoformat()
    assert home["published"] == {
        yesterday.isoformat(): {
            "id": submitted.id,
            "closing_balance": 120.0,
            "bank_deposit": 50.0,
            "xero_integrated": True,
            "submitter_first_name": "Olive",
        }
    }
    assert home["today_is_latest_report"] is True and home["today_has_report"] is False


def test_the_calendar_floor_is_the_earliest_draft(client, shop):
    make_report(shop.entity, TODAY, status="draft")
    make_report(shop.entity, TODAY - timedelta(days=3), status="draft")

    home = _home(client, shop)

    assert home["draft_date"] == (TODAY - timedelta(days=3)).isoformat()
    assert home["latest"]["date"] == TODAY.isoformat()  # the newest draft drives the card


def test_the_onboarding_start_date_is_the_first_reports_floor(client, shop):
    start = TODAY - timedelta(days=5)
    EntityPettycashSettings.objects.create(entity_id=shop.entity.id, opening_balance=500, start_date=start)

    home = _home(client, shop)

    # onboarding seeds no draft, so the start date is the calendar's only floor
    assert home["start_date"] == start.isoformat()
    assert home["draft_date"] is None


def test_today_submitted(client, shop):
    report = make_report(shop.entity, TODAY, closing_balance=10)

    home = _home(client, shop)

    assert home["today_has_report"] is True and home["today_is_latest_report"] is True
    assert home["published"][TODAY.isoformat()]["id"] == report.id


def test_a_deposit_that_cannot_be_corrected_says_why(client, shop):
    make_report(shop.entity, TODAY - timedelta(days=1), closing_balance=10, bank_deposit=25)

    latest = _home(client, shop)["latest"]

    assert latest["deposit"] == 25.0
    assert latest["deposit_bank_name"] == "Unknown Bank" and latest["deposit_bank_number"] == "****"
    assert latest["can_correct_deposit"] is False
    assert latest["deposit_correction_missing_settings"] == ["Deposit Bank Account", "Petty Cash Account"]


def test_a_draft_with_no_deposit_entered_yet_reads_as_none(client, shop):
    """A legacy draft row with ``bank_deposit`` NULL (Flask's ``> 0`` would have raised)."""
    make_report(shop.entity, TODAY, status="draft", bank_deposit=None)

    latest = _home(client, shop)["latest"]

    assert latest["deposit"] == 0.0 and latest["deposit_bank_name"] == "No deposit"


def test_a_superuser_outside_the_company_reads_it_read_only(client, shop):
    staff = make_user("staff@test.com", system_role=SystemRole.SUPERADMIN)

    assert _home(client, shop, staff)["permissions"]["read_only"] is True


def test_a_cashier_may_read_but_not_correct_a_deposit(client, shop):
    cashier = _member(shop, "cashier@test.com")

    assert _home(client, shop, cashier)["permissions"] == {"read_only": False, "correct_deposit": False}


def test_the_user_count_is_the_companys_approved_members(client, shop):
    _member(shop, "cashier@test.com")
    _member(shop, "pending@test.com", approved=False)

    assert _home(client, shop)["user_count"] == 2  # the owner and the cashier


# --- stale drafts (2026-10-26) -------------------------------------------------------------------------
# A draft dated before the last submitted day can never be finished in order. It used to hide Start New
# Report and send Continue to its old date, blocking the company (Dine at Venus, 2026-10-20).


def test_a_draft_older_than_the_last_submitted_day_is_not_the_days_draft(client, shop):
    yesterday = TODAY - timedelta(days=1)
    make_report(shop.entity, yesterday, created_by=shop.owner.id, closing_balance=120)
    make_report(shop.entity, TODAY - timedelta(days=9), status="draft", current_section="sales")

    home = _home(client, shop)

    assert home["draft"] is None and home["draft_date"] is None
    assert home["latest"]["date"] == yesterday.isoformat()


def test_a_stale_draft_beside_todays_draft_leaves_todays_in_charge(client, shop):
    make_report(shop.entity, TODAY - timedelta(days=1), created_by=shop.owner.id, closing_balance=120)
    make_report(shop.entity, TODAY - timedelta(days=9), status="draft")
    current = make_report(shop.entity, TODAY, status="draft", current_section="sales")

    home = _home(client, shop)

    assert home["draft"]["id"] == current.id
    assert home["draft_date"] == TODAY.isoformat()
