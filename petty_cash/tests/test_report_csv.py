"""History's movements CSV (``GET /reports/export.csv``, ``services/report_csv.py``) - Flask's
``tests/test_report_csv_export.py`` expectations for the API, restated. The bytes are Flask's."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from minty_db.enums import EntityRole
from minty_db.models import (
    AccountInfo,
    Entity,
    EntityPettycashSettings,
    ReportExpense,
    UserEntity,
)

from core import policy
from petty_cash.tests.conftest import WEB, headers, make_entity, make_report, make_user, url
from petty_cash.tests.test_close import D3

pytestmark = pytest.mark.django_db

HEADER = ["Date", "Account Code", "Amount", "Description", "Reference", "Check Number"]


def _day(days_back):
    return (D3 - timedelta(days=days_back)).isoformat()


def _range(days_back_start=2, days_back_end=0):
    return {"start_date": _day(days_back_start), "end_date": _day(days_back_end)}


def _account(entity, code, name):
    return AccountInfo.objects.create(id=uuid.uuid4(), entity_id=entity.id, type="EXPENSE", name=name,
                                      xero_account_id=str(uuid.uuid4()), xero_code=code, status="ACTIVE")


@pytest.fixture
def books(shop):
    """Flask's ``books``: two submitted days and a draft before them, every account role set -
    petty cash 090, owner's account 880, cash sales 200, cash difference 870."""
    entity = shop.entity
    petty, director = _account(entity, "090", "Petty Cash"), _account(entity, "880", "Owner")
    sale, difference = _account(entity, "200", "Sales"), _account(entity, "870", "Difference")
    tape = _account(entity, "445", "Tape")
    EntityPettycashSettings.objects.create(
        entity_id=entity.id, pettycash_account_id=petty.id, director_account_id=director.id,
        cash_sale_account_id=sale.id, discrepancy_account_id=difference.id)
    base = datetime(2026, 10, 1, 9, tzinfo=UTC)

    def report(days_back, status="submitted", **fields):
        return make_report(entity, D3 - timedelta(days=days_back), status=status, created_by=shop.owner.id,
                           opening_balance=0, closing_balance=0, **fields)

    def line(row, minutes, item, amount, remarks, account=None):
        ReportExpense.objects.create(id=uuid.uuid4(), report_id=row.id, item=item, amount=Decimal(str(amount)),
                                     remarks=remarks, account_id=account.id if account else None,
                                     created_at=base + timedelta(minutes=minutes))

    draft = report(2, status="draft", cashsale_total=99)
    line(draft, 0, "Draft line", 7, "Draft")
    first = report(1, cash_addition=20, cash_addition_type="personal", cashsale_total=300, bank_deposit=500,
                   discrepancy_amount=Decimal("2.5"), discrepancy_type="short")
    line(first, 2, "Blank", 10, "No description")
    line(first, 1, "Tape", "25.10", "Tape, the wide roll", tape)
    line(first, 3, "Dash", 0, "-")
    second = report(0, cash_addition=50, cash_addition_type="company", discrepancy_amount=1, discrepancy_type="over")
    line(second, 0, "Tea", "12.5", "Café 茶")
    return shop


SUBMITTED_ROWS = [
    [_day(1), "880", "20.0", "Cash Addition/Withdrawal From (Start)", " ", " "],
    [_day(1), "445", "-25.1", "Tape, the wide roll", " ", " "],
    [_day(1), "N/A", "-10.0", "Expense", " ", " "],
    [_day(1), "N/A", "0.0", "Expense", " ", " "],  # a zero line is 0.0, never -0.0
    [_day(1), "200", "300.0", "Cash Sale (Pettycash)", " ", " "],
    [_day(1), "090", "-500.0", "Bank Deposit", " ", " "],
    [_day(1), "870", "-2.5", "Discrepancy (short)", " ", " "],
    [_day(0), "090", "50.0", "Cash Addition/Withdrawal From (Start)", " ", " "],
    [_day(0), "N/A", "-12.5", "Café 茶", " ", " "],
    [_day(0), "870", "1.0", "Discrepancy (over)", " ", " "],
]


def _bytes(rows) -> bytes:
    lines = [",".join(f'"{c}"' if "," in c else c for c in r) for r in [HEADER] + rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _get(client, entity, user, **query):
    return client.get(url(entity.id, "/reports/export.csv"), query, headers=headers(user.id))


def test_the_file_lists_each_movement_of_the_submitted_days(client, books):
    resp = _get(client, books.entity, books.owner, **_range(2, 0))

    assert resp.status_code == 200, resp.content[:300]
    assert resp.content == _bytes(SUBMITTED_ROWS)  # the draft's day is left out
    assert resp["Content-Type"] == "text/csv; charset=utf-8"
    assert resp["Content-Disposition"] == f"attachment; filename=acme-shop_reports_{_day(2)}_to_{_day(0)}.csv"
    assert resp["Access-Control-Allow-Origin"] == WEB
    assert resp["Access-Control-Expose-Headers"] == "Content-Disposition"
    assert resp["Cache-Control"] == "no-store"


def test_no_settings_leave_the_codes_blank(client, books):
    EntityPettycashSettings.objects.filter(entity_id=books.entity.id).delete()

    body = _get(client, books.entity, books.owner, **_range(1, 0)).content.decode("utf-8")

    codes = [r[1] for r in list(csv.reader(io.StringIO(body)))[1:]]
    assert codes == ["", "445", "N/A", "N/A", "", "", "", "", "N/A", ""]


def test_the_export_is_not_taken_for_a_report_id(client, books):
    """``/reports/{report_id}`` is mounted last; the export answers its own path."""
    resp = _get(client, books.entity, books.owner, **_range(1, 1))
    assert resp.status_code == 200 and resp["Content-Type"].startswith("text/csv")


def test_a_company_name_outside_ascii_travels_as_filename_star(client, books):
    Entity.objects.filter(id=books.entity.id).update(name="茶餐廳 & Co")

    resp = _get(client, books.entity, books.owner, **_range(1, 1))

    tail = f"_reports_{_day(1)}_to_{_day(1)}.csv"
    assert resp["Content-Disposition"] == (
        f"attachment; filename=and-co{tail}; filename*=UTF-8''%E8%8C%B6%E9%A4%90%E5%BB%B3-and-co{tail}")


@pytest.mark.parametrize("query, message", [
    ({}, "Choose both dates to export the reports between them."),
    ({"start_date": "2026-10-01"}, "Choose both dates to export the reports between them."),
    ({"start_date": "01/10/2026", "end_date": "2026-10-02"},
     "A date isn't in the YYYY-MM-DD form. Choose it from the calendar."),
    ({"start_date": "2026-10-05", "end_date": "2026-10-02"},
     "The start date is after the end date. Choose a start on or before the end."),
    ({"start_date": "2025-01-01", "end_date": "2026-01-02"},
     "The range is longer than a year. Choose 366 days or fewer."),
])
def test_a_bad_range_is_refused(client, books, query, message):
    resp = _get(client, books.entity, books.owner, **query)
    assert resp.status_code == 422 and resp.json() == {"error": message}


def test_a_year_of_366_days_is_allowed(client, books):
    resp = _get(client, books.entity, books.owner, start_date=_day(365), end_date=_day(0))
    assert resp.status_code == 200


def test_nothing_to_export_is_404(client, books):
    for query in ({"start_date": "2020-01-01", "end_date": "2020-01-31"}, _range(2, 2)):  # nothing; a draft only
        resp = _get(client, books.entity, books.owner, **query)
        assert resp.status_code == 404 and resp.json() == {
            "error": "There are no submitted reports between those dates. Choose another range."}


def test_it_needs_the_company_wide_view(client, books, monkeypatch):
    base = make_user("base@test.com", first_name="Bea")
    UserEntity.objects.create(user_id=base.id, entity_id=books.entity.id, role=EntityRole.ENTITY_BASE, approved=True)
    resp = _get(client, books.entity, base, **_range())
    assert resp.status_code == 403 and resp.json() == {
        "error": "You don't have permission to export this company's reports."}

    cashier = make_user("cashier@test.com", first_name="Cass")
    UserEntity.objects.create(user_id=cashier.id, entity_id=books.entity.id, role=EntityRole.CASHIER, approved=True)
    assert _get(client, books.entity, cashier, **_range()).status_code == 200
    monkeypatch.setitem(policy.PERMISSION_RULES, policy.Permission.REPORT_VIEW_ENTITY,
                        policy.PermissionRule(policy.Role.SHOP_MANAGER))
    assert _get(client, books.entity, cashier, **_range()).status_code == 403


def test_another_companys_export_is_refused_at_the_door(client, books):
    stranger = make_user("stranger@test.com", first_name="Sam")
    theirs = make_entity(stranger, name="Their Shop")
    assert _get(client, theirs, books.owner, **_range()).status_code == 403
