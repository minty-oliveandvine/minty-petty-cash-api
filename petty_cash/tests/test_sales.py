"""The Sales step (``petty_cash/routes/sales.py``) - Flask's ``tests/test_petty_cash_api_sales.py``
and ``tests/test_report_sales_state.py`` expectations, restated."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from minty_db.enums import EntityRole, SaleType
from minty_db.models import (
    EntitySaleSetting,
    Report,
    ReportHistory,
    ReportSale,
    SaleInfo,
    UserEntity,
)

from petty_cash.days import today
from petty_cash.tests.conftest import headers, make_report, make_user, url

pytestmark = pytest.mark.django_db
D3 = today() - timedelta(days=1)

#: Flask's ``create_default_entity_settings``: (name, value_name, type, order).
DEFAULTS = [
    ("Cash", "cash_sales", SaleType.OTHER, 0),
    ("Visa", "visa_sales", SaleType.ELECTRONIC, 1),
    ("Alipay", "alipay_sales", SaleType.ELECTRONIC, 2),
    ("WeChat Pay", "wechat_sales", SaleType.ELECTRONIC, 3),
    ("Mastercard", "master_sales", SaleType.ELECTRONIC, 4),
    ("UnionPay", "unionpay_sales", SaleType.ELECTRONIC, 5),
    ("Amex", "amex_sales", SaleType.ELECTRONIC, 6),
    ("Octopus", "octopus_sales", SaleType.ELECTRONIC, 7),
    ("Food Panda", "foodpanda_sales", SaleType.DELIVERY, 1),
    ("Keeta", "keeta_sales", SaleType.DELIVERY, 2),
    ("OpenRice", "openrice_sales", SaleType.DELIVERY, 3),
]


@pytest.fixture
def methods(shop):
    rows = {}
    for name, value_name, sale_type, order in DEFAULTS:
        row = SaleInfo.objects.create(sale_name=name, value_name=value_name, type=sale_type, display_order=order)
        EntitySaleSetting.objects.create(entity_id=shop.entity.id, sale_id=row.id, is_active=True, display_order=order)
        rows[value_name] = row
    return rows


def _draft(shop, **fields):
    values = {"opening_balance": 100, "cash_addition": 20, "adjusted_opening_balance": 120,
              "closing_balance": 120, "completed_sections": ["opening"], "current_section": "sales"}
    values.update(fields)
    return make_report(shop.entity, D3, status=values.pop("status", "draft"), created_by=shop.owner.id, **values)


def _set(report, methods, amounts):
    for value_name, value in amounts.items():
        ReportSale.objects.create(report_id=report.id, sale_id=methods[value_name].id, amount=value)


def _amounts(report):
    names = dict(SaleInfo.objects.values_list("id", "value_name"))
    return {names[s]: a for s, a in ReportSale.objects.filter(report_id=report.id).values_list("sale_id", "amount")}


def _get(client, shop, day=D3, user=None):
    query = {"transaction_date": day.isoformat()} if day else {}
    return client.get(url(shop.entity.id, "/reports/sales"), query, headers=headers((user or shop.owner).id))


def _put(client, shop, body, user=None):
    return client.put(url(shop.entity.id, "/reports/sales"), body, content_type="application/json",
                      headers=headers((user or shop.owner).id))


def _body(**over):
    return {"transaction_date": D3.isoformat(), "cash_sales": 0, "amounts": {}, "action": "next", **over}


# --- GET ---------------------------------------------------------------------------------------------


def test_the_methods_come_in_settings_order_with_the_days_amounts(client, shop, methods):
    draft = _draft(shop, cashsale_total=50)
    _set(draft, methods, {"cash_sales": 50, "visa_sales": 30, "keeta_sales": 4.5})

    resp = _get(client, shop)

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["transaction_date"] == D3.isoformat() and body["currency_code"] == "HKD"
    assert body["draft"]["id"] == str(draft.id) and body["draft"]["completed_sections"] == ["opening"]
    by_type = lambda t: [m["name"] for m in body["methods"] if m["type"] == t]  # noqa: E731
    assert by_type("electronic") == ["Visa", "Alipay", "WeChat Pay", "Mastercard", "UnionPay", "Amex", "Octopus"]
    assert by_type("delivery") == ["Food Panda", "Keeta", "OpenRice"]
    assert {m["type"] for m in body["methods"]} == {"electronic", "delivery"}  # Cash is cash_sales
    amounts = {m["value_name"]: m["amount"] for m in body["methods"]}
    assert amounts["visa_sales"] == 30 and amounts["keeta_sales"] == 4.5 and amounts["alipay_sales"] is None
    assert body["cash_sales"] == 50
    # the methods interleave by Settings order across both types, as Flask's query returns them
    assert [m["name"] for m in body["methods"]][:3] == ["Food Panda", "Visa", "Alipay"]


def test_with_no_draft_the_page_reads_empty(client, shop, methods):
    body = _get(client, shop).json()

    assert body["draft"] is None and body["cash_sales"] is None
    assert all(m["amount"] is None for m in body["methods"])


def test_a_missing_or_bad_date_and_a_member_without_the_right_are_refused(client, shop, methods):
    assert _get(client, shop, day=None).status_code == 422
    base = make_user("base@test.com")
    UserEntity.objects.create(user_id=base.id, entity_id=shop.entity.id, role=EntityRole.ENTITY_BASE, approved=True)

    assert _get(client, shop, user=base).status_code == 403
    assert _put(client, shop, _body(), user=base).status_code == 403


# --- PUT ---------------------------------------------------------------------------------------------


def test_save_next_writes_every_method_and_moves_on_to_expenses(client, shop, methods):
    draft = _draft(shop, expense_total=15, bank_deposit=5)
    _set(draft, methods, {"alipay_sales": 80})

    resp = _put(client, shop, _body(cash_sales=1000.5, amounts={"visa_sales": 200, "foodpanda_sales": 33.25}))

    assert resp.status_code == 200, resp.content
    assert resp.json() == {"draft_id": str(draft.id), "transaction_date": D3.isoformat(), "current_section": "expenses",
                           "completed_sections": ["opening", "sales"], "next": "expenses"}
    amounts = _amounts(draft)
    assert amounts["cash_sales"] == Decimal("1000.50") and amounts["visa_sales"] == 200
    assert amounts["foodpanda_sales"] == Decimal("33.25")
    assert amounts["alipay_sales"] == 0 and amounts["openrice_sales"] == 0  # every shown method gets a row
    row = Report.objects.get(id=draft.id)
    assert (row.cashsale_total, row.nocashsale_total, row.total_sales) == (
        Decimal("1000.50"), Decimal("233.25"), Decimal("1233.75"))
    assert row.closing_balance == Decimal("100") + 20 + Decimal("1000.50") - 15 - 5
    history = list(ReportHistory.objects.filter(report_id=draft.id).values_list("action", "field_changed", "new_value"))
    assert history == [("updated", "sales_entry",
                        "Sales: Shop=1200.5, Delivery=33.25, Total=1233.75, Closing Balance=1100.5")]


def test_save_exit_stays_on_sales(client, shop, methods):
    _draft(shop)

    body = _put(client, shop, _body(cash_sales=5, action="exit")).json()

    assert body["next"] == "dashboard" and body["current_section"] == "sales"


def test_saving_again_closes_on_the_cash_just_typed(client, shop, methods):
    draft = _draft(shop)

    _put(client, shop, _body(cash_sales=50))
    assert Report.objects.get(id=draft.id).closing_balance == 170
    _put(client, shop, _body(cash_sales=80))

    assert Report.objects.get(id=draft.id).closing_balance == 200  # 100 + 20 + 80


def test_a_method_the_page_does_not_show_keeps_its_amount(client, shop, methods):
    tips = SaleInfo.objects.create(sale_name="Tips", value_name="tips_sales", type=SaleType.OTHER)
    EntitySaleSetting.objects.create(entity_id=shop.entity.id, sale_id=tips.id, is_active=True, display_order=9)
    draft = _draft(shop)
    ReportSale.objects.create(report_id=draft.id, sale_id=tips.id, amount=7)

    _put(client, shop, _body(cash_sales=5))

    assert _amounts(draft)["tips_sales"] == 7


def test_a_save_makes_the_saver_the_reports_creator(client, shop, methods):
    clerk = make_user("clerk@test.com")
    UserEntity.objects.create(user_id=clerk.id, entity_id=shop.entity.id, role=EntityRole.ADMIN, approved=True)
    draft = _draft(shop)

    _put(client, shop, _body(cash_sales=1), user=clerk)

    assert Report.objects.get(id=draft.id).created_by == clerk.id


def test_with_no_draft_the_save_goes_back_to_opening(client, shop, methods):
    resp = _put(client, shop, _body(cash_sales=5))

    assert resp.status_code == 409
    assert resp.json() == {"error": "I don't see a draft yet - let's start with the opening entry.", "to": "opening"}


@pytest.mark.parametrize("over, message", [
    ({"transaction_date": "14/10/2026"}, "That date doesn't look quite right!"),
    ({"cash_sales": -1}, "Enter the cash sales as an amount of 0 or more."),
    ({"cash_sales": "12"}, "Enter the cash sales as an amount of 0 or more."),
    ({"amounts": {"visa_sales": -3}}, "Enter the Visa sales as an amount of 0 or more."),
    ({"amounts": {"visa_sales": 100_000_000_000}}, "Enter the Visa sales as an amount of 0 or more."),
    ({"amounts": {"cash_sales": 3}}, "isn't switched on for this company"),
    ({"amounts": {"made_up_sales": 3}}, "isn't switched on for this company"),
    ({"amounts": [1]}, "Send the amounts as one per sales method."),
    ({"action": "later"}, "Choose Save & Next or Save & Exit."),
])
def test_bad_input_is_a_422(client, shop, methods, over, message):
    draft = _draft(shop)

    resp = _put(client, shop, _body(**over))

    assert resp.status_code == 422
    assert message in resp.json()["error"]
    assert _amounts(draft) == {}
