"""A company keeping its books in Minty with no Xero (Xero optional, 2026-11-10; minty-db v0.5's
``entities.setup_mode = 'minty'``). Onboarding's Accounting Setup chose its accounts; here:

- Setup Required asks only for the five accounts daily closing needs, in onboarding's words - not
  the three Xero contacts, and the discrepancy bank follows the petty cash account as it does for
  a Xero company;
- a supplier added on the expense form is kept in Minty, with no Xero call;
- publishing is refused in one sentence, before any Xero call.
"""

from __future__ import annotations

import datetime as dt

import pytest
from minty_db.models import AccountInfo, Entity, EntityPettycashSettings, XeroContactSync

from petty_cash.services import publish as pub
from petty_cash.services import suppliers
from petty_cash.services.xero_settings import missing_slots
from petty_cash.tests.conftest import headers, make_report, url

pytestmark = pytest.mark.django_db


@pytest.fixture
def minty(shop):
    Entity.objects.filter(id=shop.entity.id).update(setup_mode="minty", xero_org_id=None)
    return shop


def _account(entity, code, name, xero_type, account_type):
    return AccountInfo.objects.create(entity_id=entity.id, name=name, type=xero_type, xero_code=code, code=code,
                                      account_type=account_type, status="ACTIVE", source="user")


def _five(entity, skip=()):
    rows = {
        "pettycash_account_id": _account(entity, "090", "Petty Cash", "BANK", "bank"),
        "bank_account_id": _account(entity, "091", "Bank Account", "BANK", "bank"),
        "cash_sale_account_id": _account(entity, "200", "Product sales", "SALES", "sales"),
        "discrepancy_account_id": _account(entity, "480", "Cash Over / Short", "EXPENSE", "expense"),
        "director_account_id": _account(entity, "800", "Amount Due to Director", "CURRLIAB", "current_liability"),
    }
    EntityPettycashSettings.objects.update_or_create(
        entity_id=entity.id, defaults={c: a.id for c, a in rows.items() if c not in skip})


def test_the_five_accounts_are_all_daily_closing_needs(minty):
    _five(minty.entity)
    assert missing_slots(minty.entity.id) == []


def test_a_missing_one_is_named_in_onboardings_words(minty):
    _five(minty.entity, skip=("discrepancy_account_id",))
    assert missing_slots(minty.entity.id) == ["Cash difference"]
    EntityPettycashSettings.objects.filter(entity_id=minty.entity.id).delete()
    assert missing_slots(minty.entity.id) == [
        "Petty cash", "Bank you deposit into", "Cash sales", "Paid personally", "Cash difference"]


def test_the_dashboard_says_setup_is_complete(client, minty):
    _five(minty.entity)
    home = client.get(url(minty.entity.id, "/home"), headers=headers(minty.owner.id)).json()
    assert home["xero_settings"] == {"complete": True, "missing": []}


def test_a_xero_company_still_needs_its_contacts(shop):
    _five(shop.entity)
    assert "Cash Sale Contact" in missing_slots(shop.entity.id)


def test_a_supplier_is_kept_in_minty_with_no_xero_call(client, minty, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("no Xero call for a company keeping its books in Minty")

    monkeypatch.setattr("requests.post", no_network)
    resp = client.post(url(minty.entity.id, "/suppliers"), {"name": " Corner  Bakery "},
                       content_type="application/json", headers=headers(minty.owner.id))
    assert resp.status_code == 201
    row = XeroContactSync.objects.get(entity_id=minty.entity.id)
    assert resp.json() == {"id": str(row.id), "name": "Corner Bakery"}
    assert (row.xero_contact_id, row.xero_org_id, row.source) == (None, None, "user")

    again = client.post(url(minty.entity.id, "/suppliers"), {"name": "corner bakery"},
                        content_type="application/json", headers=headers(minty.owner.id))
    assert again.status_code == 201 and again.json()["id"] == str(row.id), "the same supplier, not a second"
    assert XeroContactSync.objects.filter(entity_id=minty.entity.id).count() == 1


def test_publishing_is_refused_before_any_xero_call(client, minty, monkeypatch):
    monkeypatch.setattr(pub, "xero_token", lambda entity_id: (_ for _ in ()).throw(AssertionError("asked Xero")))
    report = make_report(minty.entity, dt.date(2026, 11, 2), created_by=minty.owner.id)
    resp = client.post(url(minty.entity.id, f"/reports/{report.id}/publish"), headers=headers(minty.owner.id))
    assert (resp.status_code, resp.json()) == (409, {"error": pub.NOT_IN_XERO})


def test_the_rule_reads_setup_mode_and_the_org(shop):
    assert not suppliers.minty_on_its_own(shop.entity.id)
    Entity.objects.filter(id=shop.entity.id).update(setup_mode="minty")
    assert suppliers.minty_on_its_own(shop.entity.id)
    Entity.objects.filter(id=shop.entity.id).update(xero_org_id="org-1")
    assert not suppliers.minty_on_its_own(shop.entity.id), "connected later: Xero's rules again"


# ---- Petty Cash Settings' mapping card, for such a company --------------------------------------


def _settings(client, shop):
    return client.get(url(shop.entity.id, "/settings/xero"), headers=headers(shop.owner.id))


def _save(client, shop, body):
    return client.put(url(shop.entity.id, "/settings/xero/mapping"), body, content_type="application/json",
                      headers=headers(shop.owner.id))


def test_the_card_offers_its_own_accounts_with_no_xero_call(client, minty, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("no Xero call for a company keeping its books in Minty")

    monkeypatch.setattr("requests.post", no_network)
    _five(minty.entity, skip=("discrepancy_account_id",))
    read = _settings(client, minty).json()
    assert read["minty"] is True and read["live"] is False
    fields = {f["key"]: f for f in read["mapping"]}
    assert list(fields) == ["main_bank", "deposit_bank", "cashsale_account", "owners_account", "discrepancy_account"]
    assert {o["name"] for o in fields["main_bank"]["options"]} == {"Petty Cash", "Bank Account"}
    assert [o["name"] for o in fields["discrepancy_account"]["options"]] == ["Cash Over / Short"]
    assert fields["discrepancy_account"]["selected"] is None
    petty = AccountInfo.objects.get(entity_id=minty.entity.id, code="090")
    assert fields["main_bank"]["selected"] == str(petty.id), "Minty's own id: there is no Xero id"


def test_choosing_the_missing_one_completes_setup(client, minty):
    _five(minty.entity, skip=("discrepancy_account_id",))
    fields = {f["key"]: f for f in _settings(client, minty).json()["mapping"]}
    body = {k: (f["selected"] or f["options"][0]["id"]) for k, f in fields.items()}
    resp = _save(client, minty, body)
    assert resp.status_code == 200 and resp.json()["message"] == "Account mapping saved."
    assert missing_slots(minty.entity.id) == []
    row = EntityPettycashSettings.objects.get(entity_id=minty.entity.id)
    assert row.discrepancy_bank_account_id == row.pettycash_account_id, "follows the petty cash account"


@pytest.mark.parametrize(("change", "message"), [
    ({"discrepancy_account": ""}, "Choose the Discrepancy account code."),
    ({"deposit_bank": "MAIN"}, "can't be the same"),
    ({"cashsale_account": "not-an-option"}, "Choose the Cash Sales account code from the list."),
])
def test_a_mapping_that_does_not_add_up_is_refused(client, minty, change, message):
    _five(minty.entity)
    fields = {f["key"]: f for f in _settings(client, minty).json()["mapping"]}
    body = {k: f["selected"] for k, f in fields.items()}
    body.update({k: (body["main_bank"] if v == "MAIN" else v) for k, v in change.items()})
    resp = _save(client, minty, body)
    assert resp.status_code == 422 and message in resp.json()["error"]


def test_a_completed_publish_freezes_what_it_sent_on_each_line(shop):
    from minty_db.models import ReportExpense

    account = _account(shop.entity, "469", "Rent", "EXPENSE", "expense")
    contact = XeroContactSync.objects.create(entity_id=shop.entity.id, xero_contact_id="c-1", name="Landlord Ltd")
    report = make_report(shop.entity, dt.date(2026, 11, 3), created_by=shop.owner.id, publishing_status="publishing")
    line = ReportExpense.objects.create(report_id=report.id, account_id=account.id, contact_id=contact.id, amount=10)

    pub._finish(report.id, shop.owner.id, {"failed": 0, "succeeded": 1})

    line = ReportExpense.objects.get(id=line.id)
    assert (line.published_account_code, line.published_account_name, line.published_contact_name) == (
        "469", "Rent", "Landlord Ltd")
    AccountInfo.objects.filter(id=account.id).update(xero_code="470", name="Shop rent")
    assert ReportExpense.objects.get(id=line.id).published_account_code == "469", "never rewritten"


def test_publish_is_never_offered_to_a_minty_company(client, minty):
    report = make_report(minty.entity, dt.date(2026, 11, 4), created_by=minty.owner.id)
    h = headers(minty.owner.id)
    outcome = client.get(url(minty.entity.id, f"/reports/{report.id}/outcome"), headers=h).json()
    assert outcome["can_publish"] is False
    assert client.get(url(minty.entity.id, "/reports"), headers=h).json()["can_publish"] is False
    page = client.get(url(minty.entity.id, f"/reports/{report.id}"), headers=h).json()
    assert page["permissions"]["publish"] is False


def test_a_xero_company_is_still_offered_publish(client, shop):
    report = make_report(shop.entity, dt.date(2026, 11, 4), created_by=shop.owner.id)
    outcome = client.get(url(shop.entity.id, f"/reports/{report.id}/outcome"), headers=headers(shop.owner.id))
    assert outcome.json()["can_publish"] is True


# ---- a company that banks all its sales (owner's brief, 2026-11-10) -----------------------------


def test_a_company_banking_all_its_sales_needs_no_cash_sale_account(client, minty):
    Entity.objects.filter(id=minty.entity.id).update(uses_sales_cash=False)
    _five(minty.entity, skip=("cash_sale_account_id",))
    assert missing_slots(minty.entity.id) == []
    home = client.get(url(minty.entity.id, "/home"), headers=headers(minty.owner.id)).json()
    assert home["xero_settings"] == {"complete": True, "missing": []}

    # With cash sales, the same company is still asked for it.
    Entity.objects.filter(id=minty.entity.id).update(uses_sales_cash=True)
    assert missing_slots(minty.entity.id) == ["Cash sales"]


def test_its_account_mapping_saves_without_a_cash_sale_account(client, minty):
    Entity.objects.filter(id=minty.entity.id).update(uses_sales_cash=False)
    _five(minty.entity)
    row = EntityPettycashSettings.objects.get(entity_id=minty.entity.id)
    body = {"main_bank": str(row.pettycash_account_id), "deposit_bank": str(row.bank_account_id),
            "cashsale_account": "", "owners_account": str(row.director_account_id),
            "discrepancy_account": str(row.discrepancy_account_id)}
    resp = client.put(url(minty.entity.id, "/settings/xero/mapping"), body, content_type="application/json",
                      headers=headers(minty.owner.id))
    assert resp.status_code == 200, resp.json()
    assert EntityPettycashSettings.objects.get(entity_id=minty.entity.id).cash_sale_account_id is None

    Entity.objects.filter(id=minty.entity.id).update(uses_sales_cash=True)
    resp = client.put(url(minty.entity.id, "/settings/xero/mapping"), body, content_type="application/json",
                      headers=headers(minty.owner.id))
    assert resp.status_code == 422 and "Cash Sales account code" in resp.json()["error"]


# ---- connecting Xero later (Settings Phase 2, 2026-11-11) ------------------------------------------


def test_publishing_waits_while_the_accounts_are_being_matched(client, minty, monkeypatch):
    """Signed in to Xero, matching not finished: the company is still Minty's, so nothing goes to Xero."""
    monkeypatch.setattr(pub, "xero_token", lambda entity_id: (_ for _ in ()).throw(AssertionError("asked Xero")))
    Entity.objects.filter(id=minty.entity.id).update(xero_org_id="org-1", status="disconnected")
    report = make_report(minty.entity, dt.date(2026, 11, 2), created_by=minty.owner.id)
    resp = client.post(url(minty.entity.id, f"/reports/{report.id}/publish"), headers=headers(minty.owner.id))
    assert (resp.status_code, resp.json()) == (409, {"error": pub.STILL_MATCHING})


def test_a_report_from_before_xero_was_connected_stays_in_minty(client, minty, monkeypatch):
    """Finish sets xero_publish_from (owner: only records from then on go to Xero)."""
    monkeypatch.setattr(pub, "xero_token", lambda entity_id: (_ for _ in ()).throw(AssertionError("asked Xero")))
    Entity.objects.filter(id=minty.entity.id).update(setup_mode="xero", xero_org_id="org-1", status="connected",
                                                     xero_publish_from=dt.date(2026, 11, 11))
    report = make_report(minty.entity, dt.date(2026, 11, 10), created_by=minty.owner.id)
    resp = client.post(url(minty.entity.id, f"/reports/{report.id}/publish"), headers=headers(minty.owner.id))
    assert (resp.status_code, resp.json()) == (409, {"error": pub.BEFORE_XERO})
