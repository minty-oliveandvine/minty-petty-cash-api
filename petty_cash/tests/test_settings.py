"""Petty Cash settings (``petty_cash/routes/settings.py``) - Flask's
``tests/test_petty_cash_api_settings.py`` expectations, restated, plus what Flask's suite doesn't
pin: the name limits the schema allows (a deliberate difference - Flask's 100-character check let a
75-100 character name through to a database error), the rename refusals, and the denominations
following a currency change."""

from __future__ import annotations

from decimal import Decimal

import pytest
from minty_db.enums import CashType, EntityRole, SaleType
from minty_db.models import (
    CashInfo,
    CountryInfo,
    Entity,
    EntityCashSetting,
    EntitySaleSetting,
    SaleInfo,
    UserEntity,
)

from petty_cash.tests.conftest import headers, make_currency, make_entity, make_user, url

pytestmark = pytest.mark.django_db


def seed_methods(entity, *, electronic=("Visa", "Alipay"), delivery=("Foodpanda",)) -> dict:
    """As Flask's ``replace_sales_methods`` writes them: a catalogue row per name (shared), a link
    per company ordered 1..n within its type. name -> sale id."""
    ids = {}
    for kind, names in ((SaleType.ELECTRONIC, electronic), (SaleType.DELIVERY, delivery)):
        for order, name in enumerate(names, start=1):
            sale = SaleInfo.objects.filter(sale_name=name).first() or SaleInfo.objects.create(
                sale_name=name, type=kind, value_name=name.lower().replace(" ", "_") + "_sales", enabled=True)
            EntitySaleSetting.objects.create(entity_id=entity.id, sale_id=sale.id, is_active=True, display_order=order)
            ids[name] = str(sale.id)
    return ids


@pytest.fixture
def methods(shop):
    sg = make_currency("SGD")
    CountryInfo.objects.create(country_code="SG", alpha3_code="SGP", country_name_en="Singapore", currency_id=sg.id,
                               is_active=True, display_order=2)
    return seed_methods(shop.entity)


def call(client, shop, method, path, body=None, user=None):
    kwargs = {"content_type": "application/json"} if body is not None else {}
    return getattr(client, method)(url(shop.entity.id, path), body, headers=headers((user or shop.owner).id), **kwargs)


def names(body, kind):
    return [m["name"] for m in body["sales_methods"][kind]]


def member(shop, role, email="cashier@test.com"):
    person = make_user(email, first_name="Cass")
    UserEntity.objects.create(user_id=person.id, entity_id=shop.entity.id, role=role, approved=True)
    return person


# --- the read ----------------------------------------------------------------------------------------


def test_the_settings_read(client, shop, methods):
    resp = call(client, shop, "get", "/settings")

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["country"]["country_code"] == "HK" and body["country"]["currency_id"] == str(shop.currency.id)
    assert {c["code"] for c in body["country"]["countries"]} == {"HK", "SG"}
    assert body["country"]["selected_country"] == "Hong Kong" and body["country"]["selected_currency"] == "HKD"
    assert names(body, "electronic") == ["Visa", "Alipay"] and names(body, "delivery") == ["Foodpanda"]
    assert {m["name"]: m["shared"] for m in body["sales_methods"]["electronic"]} == {"Visa": False, "Alipay": False}
    assert body["xero_connected"] is False and all(body["permissions"].values())


def test_a_method_another_company_uses_is_marked_shared(client, shop, methods):
    seed_methods(make_entity(shop.owner, name="Other Co"), electronic=("Visa",), delivery=())

    body = call(client, shop, "get", "/settings").json()

    assert {m["name"]: m["shared"] for m in body["sales_methods"]["electronic"]} == {"Visa": True, "Alipay": False}


def test_cash_is_never_listed(client, shop, methods):
    cash = SaleInfo.objects.create(sale_name="Cash", type=SaleType.OTHER, value_name="cash_sales")
    EntitySaleSetting.objects.create(entity_id=shop.entity.id, sale_id=cash.id, is_active=True, display_order=0)

    body = call(client, shop, "get", "/settings").json()

    assert "Cash" not in names(body, "electronic") + names(body, "delivery")
    assert "Cash" not in [m["name"] for group in body["available"].values() for m in group]


def test_a_cashier_reads_settings_but_may_change_nothing(client, shop, methods):
    resp = call(client, shop, "get", "/settings", user=member(shop, EntityRole.CASHIER))

    assert resp.status_code == 200
    permissions = resp.json()["permissions"]
    assert permissions.pop("view_sales_methods") is True and not any(permissions.values())


def test_a_base_member_cannot_open_settings(client, shop, methods):
    resp = call(client, shop, "get", "/settings", user=member(shop, EntityRole.ENTITY_BASE, "base@test.com"))

    assert resp.status_code == 403


# --- country & currency ------------------------------------------------------------------------------


def test_saving_the_country_brings_its_currency_and_its_notes_and_coins(client, shop, methods):
    sgd = CountryInfo.objects.get(country_code="SG").currency_id
    ten = CashInfo.objects.create(currency_id=sgd, cash_value=Decimal("10"), type=CashType.NOTE, display_order=1)
    hkd_note = CashInfo.objects.create(currency_id=shop.currency.id, cash_value=Decimal("20"), type=CashType.NOTE,
                                       display_order=1)
    EntityCashSetting.objects.create(entity_id=shop.entity.id, cash_id=hkd_note.id, is_active=True, display_order=1)

    resp = call(client, shop, "put", "/settings/country", {"country_code": "sg"})

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["message"] == "Country and currency saved."
    assert (body["country"]["country_code"], body["country"]["currency_id"]) == ("SG", str(sgd))
    org = Entity.objects.get(id=shop.entity.id)
    assert org.currency_format == "$"
    assert list(EntityCashSetting.objects.filter(entity_id=org.id).values_list("cash_id", flat=True)) == [ten.id]


def test_an_explicit_currency_wins_over_the_countrys(client, shop, methods):
    eur = make_currency("EUR")

    call(client, shop, "put", "/settings/country", {"country_code": "SG", "currency_id": str(eur.id)})

    org = Entity.objects.get(id=shop.entity.id)
    assert (org.country_code, org.currency_id) == ("SG", eur.id)


@pytest.mark.parametrize("body, message", [
    ({}, "Choose a country or a currency to save."),
    ({"country_code": "ZZ"}, "Pick one of the countries offered."),
    ({"currency_id": "not-a-uuid"}, "Pick one of the currencies offered."),
    ({"currency_id": "6d0b6d55-0000-4000-8000-000000000000"}, "Pick one of the currencies offered."),
])
def test_a_bad_country_or_currency_is_refused(client, shop, methods, body, message):
    resp = call(client, shop, "put", "/settings/country", body)

    assert resp.status_code == 422 and resp.json() == {"error": message}


def test_a_cashier_may_not_change_the_country(client, shop, methods):
    resp = call(client, shop, "put", "/settings/country", {"country_code": "SG"}, user=member(shop, EntityRole.CASHIER))

    assert resp.status_code == 403 and Entity.objects.get(id=shop.entity.id).country_code == "HK"


# --- sales methods -----------------------------------------------------------------------------------


def test_adding_a_method_from_the_catalogue_or_by_name(client, shop, methods):
    octopus = SaleInfo.objects.create(sale_name="Octopus", type=SaleType.ELECTRONIC, value_name="octopus_sales")
    offered = call(client, shop, "get", "/settings").json()["available"]["electronic"]
    assert str(octopus.id) in [m["id"] for m in offered]

    added = call(client, shop, "post", "/sales-methods", {"type": "electronic", "sale_info_id": str(octopus.id)})
    assert added.status_code == 201, added.json()
    assert names(added.json(), "electronic") == ["Visa", "Alipay", "Octopus"]
    assert added.json()["message"] == "Octopus added."

    typed = call(client, shop, "post", "/sales-methods", {"type": "delivery", "name": " Keeta "})
    assert typed.status_code == 201 and names(typed.json(), "delivery") == ["Foodpanda", "Keeta"]
    keeta = SaleInfo.objects.get(sale_name="Keeta")
    assert (keeta.type, keeta.value_name) == ("delivery", "keeta_sales")


def test_a_removed_method_added_again_switches_its_link_back_on(client, shop, methods):
    call(client, shop, "delete", f"/sales-methods/{methods['Visa']}")

    again = call(client, shop, "post", "/sales-methods", {"type": "electronic", "sale_info_id": methods["Visa"]})

    assert again.status_code == 201 and "Visa" in names(again.json(), "electronic")
    assert EntitySaleSetting.objects.filter(entity_id=shop.entity.id, sale_id=methods["Visa"]).count() == 1


@pytest.mark.parametrize("body, status, message", [
    ({"type": "other", "name": "Cheque"}, 422, "Choose Electronic or Delivery for the method."),
    ({"type": "electronic", "name": "  "}, 422, "The method's name is still empty. Type a name to add it."),
    ({"type": "electronic", "sale_info_id": "nope"}, 422, "Pick one of the methods offered, or type a new name."),
    ({"type": "electronic", "name": "visa"}, 409, "visa is already on the list."),
])
def test_a_bad_method_is_refused(client, shop, methods, body, status, message):
    resp = call(client, shop, "post", "/sales-methods", body)

    assert resp.status_code == status and resp.json() == {"error": message}


def test_rule_a_new_name_must_fit_the_catalogue(client, shop, methods):
    """Deliberate difference from Flask (100): a new method's value_name is its name + "_sales" and
    both columns hold 80 characters, so 74 is the most a new name can be."""
    longest = "x" * 74
    assert call(client, shop, "post", "/sales-methods", {"type": "electronic", "name": longest}).status_code == 201
    assert SaleInfo.objects.get(sale_name=longest).value_name == longest + "_sales"

    resp = call(client, shop, "post", "/sales-methods", {"type": "electronic", "name": "y" * 75})
    assert resp.status_code == 422
    assert resp.json() == {"error": "That name is too long. Keep it to 74 characters or fewer."}


def test_rule_a_rename_must_fit_the_catalogue(client, shop, methods):
    assert call(client, shop, "put", f"/sales-methods/{methods['Alipay']}", {"name": "z" * 80}).status_code == 200

    resp = call(client, shop, "put", f"/sales-methods/{methods['Alipay']}", {"name": "z" * 81})
    assert resp.status_code == 422 and resp.json()["error"] == "That name is too long. Keep it to 80 characters or fewer."


def test_renaming_removing_and_ordering(client, shop, methods):
    visa, alipay = methods["Visa"], methods["Alipay"]

    ordered = call(client, shop, "put", "/sales-methods/order", {"type": "electronic", "method_ids": [alipay, visa]})
    assert ordered.status_code == 200, ordered.json()
    assert names(ordered.json(), "electronic") == ["Alipay", "Visa"] and names(ordered.json(), "delivery") == ["Foodpanda"]

    stale = call(client, shop, "put", "/sales-methods/order", {"type": "electronic", "method_ids": [visa]})
    assert stale.status_code == 409

    renamed = call(client, shop, "put", f"/sales-methods/{alipay}", {"name": "AlipayHK"})
    assert renamed.status_code == 200, renamed.json()
    assert names(renamed.json(), "electronic") == ["AlipayHK", "Visa"] and renamed.json()["message"] == "Name saved."

    removed = call(client, shop, "delete", f"/sales-methods/{visa}")
    assert removed.status_code == 200 and names(removed.json(), "electronic") == ["AlipayHK"]
    assert visa in [m["id"] for m in removed.json()["available"]["electronic"]]
    assert EntitySaleSetting.objects.get(entity_id=shop.entity.id, sale_id=visa).is_active is False  # the link stays


def test_a_shared_method_or_a_taken_name_is_not_renamed(client, shop, methods):
    seed_methods(make_entity(shop.owner, name="Other Co"), electronic=("Visa",), delivery=())

    shared = call(client, shop, "put", f"/sales-methods/{methods['Visa']}", {"name": "VISA Card"})
    assert shared.status_code == 409
    assert shared.json() == {"error": "This method is shared with other companies; add a new method instead of renaming it."}

    taken = call(client, shop, "put", f"/sales-methods/{methods['Alipay']}", {"name": "foodpanda"})
    assert taken.status_code == 409 and taken.json() == {"error": "A method with that name already exists."}
    assert SaleInfo.objects.get(id=methods["Alipay"]).sale_name == "Alipay"


def test_cash_and_unknown_methods_are_not_found(client, shop, methods):
    cash = SaleInfo.objects.create(sale_name="Cash", type=SaleType.OTHER, value_name="cash_sales")
    EntitySaleSetting.objects.create(entity_id=shop.entity.id, sale_id=cash.id, is_active=True, display_order=0)

    for method_id in (str(cash.id), "6d0b6d55-0000-4000-8000-000000000000", "nope"):
        assert call(client, shop, "delete", f"/sales-methods/{method_id}").status_code == 404
        assert call(client, shop, "put", f"/sales-methods/{method_id}", {"name": "X"}).status_code == 404


def test_a_cashier_may_not_change_the_methods(client, shop, methods):
    person = member(shop, EntityRole.CASHIER)

    assert call(client, shop, "post", "/sales-methods", {"type": "electronic", "name": "Octopus"},
                user=person).status_code == 403
    assert call(client, shop, "delete", f"/sales-methods/{methods['Visa']}", user=person).status_code == 403
