"""Petty Cash Settings' Xero cards (``petty_cash/routes/settings.py``): ``GET /settings/xero``,
``PUT /settings/xero/mapping`` and ``PUT /settings/xero/account-codes`` are done by Flask's internal
route (``core/accounts_api.py::forward_xero_settings``, the owner's choice: this service holds no
Xero code) after this service checks the person's rights. Flask's
``tests/test_petty_cash_api_xero_settings.py`` expectations, restated with Flask faked (Flask's side
is Minty's ``tests/test_petty_cash_internal_xero_settings.py``)."""

from __future__ import annotations

import jwt
import pytest
import requests
from django.conf import settings
from minty_db.enums import EntityRole

from core.accounts_api import XERO_SETTINGS_UNCONFIRMED, XERO_SETTINGS_UNREACHABLE
from petty_cash.tests.conftest import headers, url
from petty_cash.tests.test_deposit_correction import _Response
from petty_cash.tests.test_publish import FakeFlask
from petty_cash.tests.test_settings import call, member

pytestmark = pytest.mark.django_db
SETTINGS = "http://flask.invalid/api/internal/xero/petty-cash-settings"
MAPPING = {"main_bank": "acc-pc", "deposit_bank": "acc-dep", "cashsale_account": "acc-sales",
           "cashsale_contact": "con-1", "owners_account": "acc-owner", "owners_contact": "con-1",
           "discrepancy_account": "acc-adv", "discrepancy_contact": "con-1"}
READ = {"live": True, "organisation": "Corner Shop Ltd", "mapping": [], "mapping_saved": False,
        "account_codes": [{"code": "400", "name": "Advertising", "selected": True}],
        "permissions": {"edit_mapping": True, "edit_account_codes": True}}
NOT_LIVE = {"live": False, "organisation": None, "mapping": None, "mapping_saved": False, "account_codes": None,
            "permissions": {"edit_mapping": True, "edit_account_codes": True}}
NO_RIGHT = "You don't have permission to change these settings. Ask an Accountant or an Admin."
#: (method, path, action, body sent)
ACTIONS = [
    ("get", "/settings/xero", "read", None),
    ("put", "/settings/xero/mapping", "mapping", MAPPING),
    ("put", "/settings/xero/account-codes", "account-codes", {"codes": ["400"]}),
]
SAVES = ACTIONS[1:]


@pytest.fixture
def flask(monkeypatch) -> FakeFlask:
    fake = FakeFlask()
    fake.answer = _Response(200, READ)
    monkeypatch.setattr("requests.post", fake.post)
    return fake


@pytest.mark.parametrize("method, path, action, body", ACTIONS)
def test_each_card_goes_to_flask_with_its_action_and_body(client, shop, flask, method, path, action, body):
    resp = call(client, shop, method, path, body)

    assert resp.status_code == 200 and resp.json() == READ
    sent, = flask.sent
    assert sent["url"] == SETTINGS and sent["json"] == {"action": action, "body": body}
    assert sent["timeout"][1] <= 60  # the read refreshes from Xero: a minute at most


@pytest.mark.parametrize("method, path, action, body", ACTIONS)
def test_the_assertion_names_this_company_and_this_person(client, shop, flask, method, path, action, body):
    call(client, shop, method, path, body)

    assertion = flask.sent[0]["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(assertion, settings.SECRET_KEY, algorithms=["HS256"])
    assert claims["scope"] == "petty-cash-xero-settings"
    assert (claims["entity_id"], claims["user_id"]) == (str(shop.entity.id), str(shop.owner.id))
    assert claims["exp"] - claims["iat"] == 60


@pytest.mark.parametrize("method, path", [("put", "/settings/xero/mapping"), ("put", "/settings/xero/account-codes")])
def test_a_save_with_no_json_object_sends_an_empty_body(client, shop, flask, method, path):
    """As Flask's ``_body()``: not an object is ``{}``, and Flask's own checks refuse it."""
    resp = getattr(client, method)(url(shop.entity.id, path), "not json", content_type="application/json",
                                   headers=headers(shop.owner.id))

    assert resp.status_code == 200
    assert flask.sent[0]["json"] == {"action": path.rsplit("/", 1)[1], "body": {}}


def test_a_company_not_live_on_xero_reads_so(client, shop, flask):
    flask.answer = _Response(200, NOT_LIVE)

    resp = call(client, shop, "get", "/settings/xero")

    assert resp.status_code == 200 and resp.json() == NOT_LIVE


def test_a_saved_mapping_comes_back_with_its_message(client, shop, flask):
    saved = {**READ, "mapping_saved": True, "message": "Xero account mapping saved.", "first_save": True}
    flask.answer = _Response(200, saved)

    resp = call(client, shop, "put", "/settings/xero/mapping", MAPPING)

    assert resp.status_code == 200 and resp.json() == saved


@pytest.mark.parametrize("method, path, action, body", SAVES)
def test_flasks_not_live_409_comes_back_as_it_is(client, shop, flask, method, path, action, body):
    refusal = {"error": "Xero isn't connected for this company. Connect it in the company's settings first."}
    flask.answer = _Response(409, refusal)

    resp = call(client, shop, method, path, body)

    assert resp.status_code == 409 and resp.json() == refusal


@pytest.mark.parametrize("path, body, message", [
    ("/settings/xero/mapping", {**MAPPING, "cashsale_contact": ""}, "Choose the Cash Sales contact."),
    ("/settings/xero/mapping", {**MAPPING, "deposit_bank": "acc-pc"},
     "The Petty Cash Account and the Deposit Bank Account can't be the same. Choose a different one for each."),
    ("/settings/xero/mapping", {**MAPPING, "deposit_bank": "acc-ghost"}, "Choose the Deposit Bank Account from the list."),
    ("/settings/xero/account-codes", {"codes": []}, "Pick at least one account code."),
    ("/settings/xero/account-codes", {"codes": "400"}, "Send the ticked account codes as a list."),
])
def test_flasks_422s_come_back_as_they_are(client, shop, flask, path, body, message):
    flask.answer = _Response(422, {"error": message})

    resp = call(client, shop, "put", path, body)

    assert resp.status_code == 422 and resp.json() == {"error": message}
    assert flask.sent[0]["json"] == {"action": path.rsplit("/", 1)[1], "body": body}


def test_a_cashier_reads_but_may_save_nothing_and_flask_is_never_asked_to(client, shop, flask):
    cashier = member(shop, EntityRole.CASHIER)

    assert call(client, shop, "get", "/settings/xero", user=cashier).status_code == 200
    for _method, path, _action, body in SAVES:
        resp = call(client, shop, "put", path, body, user=cashier)
        assert resp.status_code == 403 and resp.json() == {"error": NO_RIGHT}
    assert [s["json"]["action"] for s in flask.sent] == ["read"]


def test_a_base_member_cannot_open_the_cards_and_flask_is_never_asked(client, shop, flask):
    base = member(shop, EntityRole.ENTITY_BASE, "base@test.com")

    resp = call(client, shop, "get", "/settings/xero", user=base)

    assert resp.status_code == 403 and resp.json() == {"error": "You don't have permission to see these settings."}
    for _method, path, _action, body in SAVES:
        assert call(client, shop, "put", path, body, user=base).status_code == 403
    assert flask.sent == []


@pytest.mark.parametrize("method, path, action, body", ACTIONS)
@pytest.mark.parametrize("failure, status", [
    (requests.ConnectionError("refused"), 503),
    (requests.ReadTimeout("slow"), 504),
    (_Response(502, text="<html>Bad gateway</html>"), 503),
    (_Response(500, {"status": "error"}), 503),
])
def test_no_usable_answer_says_so(client, shop, flask, method, path, action, body, failure, status):
    """A read changed nothing - try again; a save may have been kept - reload to see."""
    flask.answer = failure

    resp = call(client, shop, method, path, body)

    expected = XERO_SETTINGS_UNREACHABLE if action == "read" else XERO_SETTINGS_UNCONFIRMED
    assert resp.status_code == status and resp.json() == {"error": expected}


def test_flask_refusing_this_services_assertion_is_our_fault_not_the_persons(client, shop, flask):
    flask.answer = _Response(401, {"error": "unauthorized"})

    resp = call(client, shop, "get", "/settings/xero")

    assert resp.status_code == 500 and resp.json() == {"error": "Something went wrong on our side. Please try again."}
