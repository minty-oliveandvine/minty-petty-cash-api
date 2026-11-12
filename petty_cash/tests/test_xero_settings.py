"""Petty Cash Settings' Xero cards (``petty_cash/routes/settings.py``, ``services/xero_cards.py``),
done here since Phase 3 of the Flask retirement.

Restated against real rows:
- Flask's ``tests/test_petty_cash_api_xero_settings.py`` and ``test_xero_mapping_state.py``;
- the live check (accounts-api's token, Xero's /connections);
- the refresh through accounts-api's one sync.

The token service is stubbed at ``xero_token``; ``requests`` answers for /connections and for
accounts-api's sync.
"""

from __future__ import annotations

import jwt
import pytest
import requests
from django.conf import settings
from minty_db.enums import EntityRole
from minty_db.models import (
    AccountInfo,
    Entity,
    EntityAccountXero,
    EntityPettycashSettings,
    XeroContactSync,
)

from core.xero_tokens import XeroToken, XeroTokenUnavailable
from petty_cash.services import xero_cards
from petty_cash.tests.test_settings import call, member

pytestmark = pytest.mark.django_db
ORG = "org-1"
MAPPING = {"main_bank": "acc-pc", "deposit_bank": "acc-dep", "cashsale_account": "acc-sales",
           "cashsale_contact": "con-1", "owners_account": "acc-owner", "owners_contact": "con-1",
           "discrepancy_account": "acc-adv", "discrepancy_contact": "con-1"}
NO_RIGHT = "You don't have permission to change these settings. Ask an Accountant or an Admin."


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


class Xero:
    """Xero's /connections and accounts-api's sync; ``listed`` = the organisation is connected."""

    def __init__(self):
        self.listed = True
        self.sync_answer = _Resp(200, {"accounts": None, "contacts": None})
        self.syncs: list[dict] = []
        self.token_problem: XeroTokenUnavailable | None = None

    def get(self, url, headers=None, timeout=None, **_):
        assert url == "https://api.xero.com/connections"
        return _Resp(200, [{"tenantId": ORG}] if self.listed else [])

    def post(self, url, headers=None, timeout=None, **_):
        assert url == f"{settings.ACCOUNTS_API_URL}/api/internal/xero/sync"
        self.syncs.append(headers)
        if isinstance(self.sync_answer, Exception):
            raise self.sync_answer
        return self.sync_answer

    def token(self, entity_id):
        if self.token_problem is not None:
            raise self.token_problem
        return XeroToken(access_token="tok", xero_org_id=ORG)


@pytest.fixture
def xero(monkeypatch) -> Xero:
    fake = Xero()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr(xero_cards, "xero_token", fake.token)
    return fake


@pytest.fixture
def lists(shop):
    """The company connected, with the accounts and the contact the mapping chooses from, and two
    Petty Cash codes (400 ticked, 404 not) - Flask's fixture."""
    Entity.objects.filter(id=shop.entity.id).update(xero_org_id=ORG, xero_tenant_name="Corner Shop Ltd",
                                                    status="connected")
    shop.entity.refresh_from_db()
    for xero_id, kind, code, name, tick in (("acc-pc", "BANK", "", "Petty Cash", None),
                                            ("acc-dep", "BANK", "", "Business Bank", None),
                                            ("acc-sales", "REVENUE", "200", "Sales", None),
                                            ("acc-owner", "CURRLIAB", "800", "Director Loan", None),
                                            ("acc-adv", "EXPENSE", "400", "Advertising", True),
                                            ("acc-fee", "EXPENSE", "404", "Bank Fees", False)):
        account = AccountInfo.objects.create(entity_id=shop.entity.id, type=kind, name=name, xero_code=code,
                                             xero_account_id=xero_id, status="ACTIVE")
        if tick is not None:
            EntityAccountXero.objects.create(account_id=account.id, name=name, type=kind, xero_account_id=xero_id,
                                             is_active=tick)
    XeroContactSync.objects.create(entity_id=shop.entity.id, xero_contact_id="con-1", name="Cash Customer")
    return shop


def _field(body, key):
    return next(f for f in body["mapping"] if f["key"] == key)


# ---- Flask's -------------------------------------------------------------------------------


def test_the_cards_read_the_lists_once_the_connection_is_live(client, lists, xero):
    body = call(client, lists, "get", "/settings/xero").json()

    assert (body["live"], body["organisation"], body["mapping_saved"]) == (True, "Corner Shop Ltd", False)
    assert len(xero.syncs) == 1
    assert [f["key"] for f in body["mapping"]] == list(MAPPING)
    assert {o["id"] for o in _field(body, "main_bank")["options"]} == {"acc-pc", "acc-dep"}
    assert _field(body, "cashsale_contact")["options"] == [{"id": "con-1", "name": "Cash Customer"}]
    assert body["account_codes"] == [{"code": "400", "name": "Advertising", "selected": True},
                                     {"code": "404", "name": "Bank Fees", "selected": False}]
    assert body["permissions"] == {"edit_mapping": True, "edit_account_codes": True}


def test_without_a_live_connection_the_accounts_codes_and_contacts_all_stay(client, lists, xero):
    """Owner, 2026-11-12: without Xero the card still shows - the five accounts from the company's
    stored chart, the account codes and (later that day) the contacts, all saved here."""
    xero.listed = False  # revoked on Xero's side: the token still works

    body = call(client, lists, "get", "/settings/xero").json()
    assert body["live"] is False and xero.syncs == []
    assert [f["key"] for f in body["mapping"]] == [
        "main_bank", "deposit_bank", "cashsale_account", "cashsale_contact", "owners_account", "owners_contact",
        "discrepancy_account", "discrepancy_contact"]
    assert {o["id"] for o in _field(body, "main_bank")["options"]} >= {"acc-pc", "acc-dep"}
    assert body["account_codes"] == [{"code": "400", "name": "Advertising", "selected": True},
                                     {"code": "404", "name": "Bank Fees", "selected": False}]
    assert Entity.objects.get(id=lists.entity.id).status == "disconnected"

    # the five accounts and the contacts save without Xero
    saved = call(client, lists, "put", "/settings/xero/mapping", MAPPING)
    assert saved.status_code == 200, saved.json()
    assert saved.json()["message"] == "Account mapping saved."
    row = EntityPettycashSettings.objects.get(entity_id=lists.entity.id)
    assert row.pettycash_account_id == AccountInfo.objects.get(xero_account_id="acc-pc").id
    assert row.discrepancy_bank_account_id == row.pettycash_account_id
    assert row.cash_sale_contact_id is not None
    assert _field(saved.json(), "main_bank")["selected"] == "acc-pc"

    resp = call(client, lists, "put", "/settings/xero/account-codes", {"codes": ["404"]})
    assert resp.status_code == 200, resp.json()
    assert resp.json()["message"] == "Account codes saved." and resp.json()["live"] is False
    assert resp.json()["account_codes"] == [{"code": "400", "name": "Advertising", "selected": False},
                                            {"code": "404", "name": "Bank Fees", "selected": True}]
    assert xero.syncs == [], "Xero is never asked"


def test_the_mapping_is_saved_and_read_back(client, lists, xero):
    resp = call(client, lists, "put", "/settings/xero/mapping", MAPPING)

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert (body["message"], body["first_save"], body["mapping_saved"]) == ("Account mapping saved.", True, True)
    assert _field(body, "deposit_bank")["selected"] == "acc-dep" and _field(body, "owners_contact")["selected"] == "con-1"
    saved = EntityPettycashSettings.objects.get(entity_id=lists.entity.id)
    assert saved.discrepancy_bank_account_id == saved.pettycash_account_id  # it mirrors the Petty Cash Account
    assert call(client, lists, "put", "/settings/xero/mapping", MAPPING).json()["first_save"] is False


@pytest.mark.parametrize("changes, message", [
    ({"cashsale_contact": ""}, "Choose the Cash Sales contact."),
    ({"deposit_bank": "acc-pc"}, "The Petty Cash Account and the Deposit Bank Account can't be the same. "
                                 "Choose a different one for each."),
    ({"deposit_bank": "acc-ghost"}, "Choose the Deposit Bank Account from the list."),
    ({"owners_contact": "con-ghost"}, "Choose the Director / Responsible person from the list."),
    ({"cashsale_account": "acc-adv"}, "Choose the Cash Sales account code from the list."),  # an expense isn't a sale
])
def test_a_mapping_that_does_not_add_up_is_refused(client, lists, xero, changes, message):
    resp = call(client, lists, "put", "/settings/xero/mapping", {**MAPPING, **changes})
    assert resp.status_code == 422 and resp.json() == {"error": message}
    assert not EntityPettycashSettings.objects.filter(entity_id=lists.entity.id).exists()


def test_the_account_codes_keep_at_least_one(client, lists, xero):
    done = call(client, lists, "put", "/settings/xero/account-codes", {"codes": ["404"]})
    assert done.status_code == 200, done.json()
    assert [c["selected"] for c in done.json()["account_codes"]] == [False, True]
    assert done.json()["message"] == "Account codes saved."

    refused = call(client, lists, "put", "/settings/xero/account-codes", {"codes": []})
    assert refused.status_code == 422 and refused.json() == {"error": "Pick at least one account code."}
    assert call(client, lists, "put", "/settings/xero/account-codes", {"codes": "400"}).status_code == 422


def test_a_cashier_reads_but_changes_nothing(client, lists, xero):
    person = member(lists, EntityRole.CASHIER)
    body = call(client, lists, "get", "/settings/xero", user=person).json()
    assert body["permissions"] == {"edit_mapping": False, "edit_account_codes": False}
    for path, body in (("/settings/xero/mapping", MAPPING), ("/settings/xero/account-codes", {"codes": ["400"]})):
        resp = call(client, lists, "put", path, body, user=person)
        assert resp.status_code == 403 and resp.json() == {"error": NO_RIGHT}


def test_a_base_member_cannot_open_the_cards(client, lists, xero):
    person = member(lists, EntityRole.ENTITY_BASE, "member@test.com")
    assert call(client, lists, "get", "/settings/xero", user=person).status_code == 403
    assert xero.syncs == []


# ---- the connection and the sync -------------------------------------------------------------


def test_the_sync_is_asked_with_this_companys_assertion(client, lists, xero):
    call(client, lists, "get", "/settings/xero")
    assertion = xero.syncs[0]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(assertion, settings.SECRET_KEY, algorithms=["HS256"])
    assert (claims["scope"], claims["entity_id"]) == ("xero-sync", str(lists.entity.id))


@pytest.mark.parametrize("failure", [requests.ConnectionError("down"), _Resp(500, {})])
def test_a_failed_sync_still_opens_the_cards_from_what_is_stored(client, lists, xero, failure):
    xero.sync_answer = failure
    body = call(client, lists, "get", "/settings/xero").json()
    assert body["live"] is True and len(body["account_codes"]) == 2


def test_a_company_with_no_working_token_is_not_live(client, lists, xero):
    xero.token_problem = XeroTokenUnavailable(reconnect=True)
    assert call(client, lists, "get", "/settings/xero").json()["live"] is False


def test_the_token_service_unreachable_is_a_503_not_not_connected(client, lists, xero):
    xero.token_problem = XeroTokenUnavailable(reconnect=False)
    resp = call(client, lists, "get", "/settings/xero")
    assert (resp.status_code, resp.json()) == (503, {"error": xero_cards.UNREACHABLE})


def test_a_company_never_connected_is_not_live_and_xero_is_never_asked(client, shop, xero):
    body = call(client, shop, "get", "/settings/xero").json()
    assert body["live"] is False and xero.syncs == []
    assert body["account_codes"] == [], "no stored accounts yet: nothing to tick"


# ---- what a save writes ----------------------------------------------------------------------


def test_a_saved_choice_no_longer_active_is_still_offered(client, lists, xero):
    call(client, lists, "put", "/settings/xero/mapping", MAPPING)
    AccountInfo.objects.filter(xero_account_id="acc-owner").update(status="INACTIVE")
    body = call(client, lists, "get", "/settings/xero").json()
    assert "acc-owner" in {o["id"] for o in _field(body, "owners_account")["options"]}
    assert _field(body, "owners_account")["selected"] == "acc-owner"


def test_an_account_is_chosen_by_its_id_or_its_code(client, lists, xero):
    resp = call(client, lists, "put", "/settings/xero/mapping", {**MAPPING, "cashsale_account": "200"})
    assert resp.status_code == 200
    saved = EntityPettycashSettings.objects.get(entity_id=lists.entity.id)
    assert AccountInfo.objects.get(id=saved.cash_sale_account_id).xero_account_id == "acc-sales"


def test_ticking_codes_lists_a_newly_eligible_account(client, lists, xero):
    AccountInfo.objects.create(entity_id=lists.entity.id, type="OVERHEADS", name="Rent", xero_code="469",
                               xero_account_id="acc-rent", status="ACTIVE")
    # Not listed yet, so it alone can't be the one ticked code (Flask's rule)...
    refused = call(client, lists, "put", "/settings/xero/account-codes", {"codes": ["469"]})
    assert refused.json() == {"error": "Pick at least one account code."}
    # ...but alongside a listed one, the save lists it ticked.
    body = call(client, lists, "put", "/settings/xero/account-codes", {"codes": ["400", "469"]}).json()
    assert {c["code"]: c["selected"] for c in body["account_codes"]} == {"400": True, "404": False, "469": True}


# ---- a company that banks all its sales (owner's brief, 2026-11-10) -----------------------------


def test_a_xero_company_banking_all_its_sales_needs_no_cash_sale_account_or_contact(client, lists, xero):
    from petty_cash.services.xero_settings import missing_slots

    Entity.objects.filter(id=lists.entity.id).update(uses_sales_cash=False)
    read = call(client, lists, "get", "/settings/xero").json()
    assert {f["key"] for f in read["mapping"] if f["optional"]} == {"cashsale_account", "cashsale_contact"}
    resp = call(client, lists, "put", "/settings/xero/mapping",
                {**MAPPING, "cashsale_account": "", "cashsale_contact": ""})
    assert resp.status_code == 200, resp.json()
    saved = EntityPettycashSettings.objects.get(entity_id=lists.entity.id)
    assert (saved.cash_sale_account_id, saved.cash_sale_contact_id) == (None, None)
    missing = missing_slots(lists.entity.id)
    assert "Cash Sale Account" not in missing and "Cash Sale Contact" not in missing
