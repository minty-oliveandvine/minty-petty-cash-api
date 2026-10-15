"""``POST /deposit-correction`` (``petty_cash/routes/home.py``, ``core/accounts_api.py``): the
dashboard's deposit correction, done by Flask's internal route (the owner's choice, 2026-10-15) -
this service checks ``REPORT_EDIT_ENTITY``, forwards the body under a signed assertion naming the
company and the person, and hands Flask's answer back unchanged. HTTP to Flask is faked; Flask's
side is Minty's ``tests/test_petty_cash_internal_deposit_correction.py``."""

from __future__ import annotations

import json

import jwt
import pytest
import requests
from django.conf import settings

from core.accounts_api import UNCONFIRMED
from petty_cash.tests.conftest import headers, make_user, url
from shared_models.enums import EntityRole
from shared_models.models import UserEntity

pytestmark = pytest.mark.django_db
FLASK = "http://flask.invalid/api/internal/xero/deposit-correction"
BODY = {"kind": "change", "date": "2026-10-13", "amount": 12.5}
DONE = {"status": "success", "message": "Previous day's deposit updated successfully.", "id": "7",
        "bank_deposit": 12.5, "next_transaction_date": "2026-10-14"}


class _Response:
    def __init__(self, status, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


class FakeFlask:
    def __init__(self):
        self.sent = []
        self.answer = _Response(200, DONE)

    def post(self, url, json=None, headers=None, timeout=None, **_):
        self.sent.append({"url": url, "json": json, "headers": headers or {}, "timeout": timeout})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def flask(monkeypatch) -> FakeFlask:
    fake = FakeFlask()
    monkeypatch.setattr("requests.post", fake.post)
    return fake


def _post(client, shop, body=BODY, user=None):
    return client.post(url(shop.entity.id, "/deposit-correction"), body, content_type="application/json",
                       headers=headers((user or shop.owner).id))


def test_the_correction_goes_to_flask_and_its_answer_comes_back(client, shop, flask):
    resp = _post(client, shop)

    assert resp.status_code == 200 and resp.json() == DONE
    sent, = flask.sent
    assert sent["url"] == FLASK and sent["json"] == BODY
    assert sent["timeout"][1] > 120  # longer than Flask's own 120 s, so a correction is never abandoned


def test_the_assertion_names_this_company_and_this_person(client, shop, flask):
    _post(client, shop)

    assertion = flask.sent[0]["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(assertion, settings.SECRET_KEY, algorithms=["HS256"])
    assert claims["scope"] == "petty-cash-deposit-correction"
    assert (claims["entity_id"], claims["user_id"]) == (str(shop.entity.id), str(shop.owner.id))
    assert claims["exp"] - claims["iat"] == 60


@pytest.mark.parametrize("status, error", [
    (400, "That deposit is larger than the previous one."),
    (409, "Cannot change or remove this deposit because the following entity setting(s) are not configured."),
    (422, "No deposit means an amount of 0."),
])
def test_flasks_refusals_come_back_as_they_are(client, shop, flask, status, error):
    flask.answer = _Response(status, {"error": error})

    resp = _post(client, shop)

    assert resp.status_code == status and resp.json() == {"error": error}


def test_a_cashier_is_refused_here_and_flask_is_never_asked(client, shop, flask):
    cashier = make_user("cashier@test.com")
    UserEntity.objects.create(user_id=cashier.id, entity_id=shop.entity.id, role=EntityRole.CASHIER, approved=True)

    resp = _post(client, shop, user=cashier)

    assert resp.status_code == 403
    assert resp.json() == {"error": "You don't have permission to change this company's reports."}
    assert flask.sent == []


@pytest.mark.parametrize("failure, status", [
    (requests.ConnectionError("refused"), 503),
    (requests.ReadTimeout("slow"), 504),
    (_Response(502, text="<html>Bad gateway</html>"), 503),
    (_Response(500, {"status": "error"}), 503),
])
def test_no_usable_answer_says_to_check_the_day(client, shop, flask, failure, status):
    """The correction may have reached Xero: never claim it did not."""
    flask.answer = failure

    resp = _post(client, shop)

    assert resp.status_code == status and resp.json() == {"error": UNCONFIRMED}


def test_flask_refusing_this_services_assertion_is_our_fault_not_the_persons(client, shop, flask):
    flask.answer = _Response(401, {"error": "unauthorized"})

    resp = _post(client, shop)

    # Not a 401: the app would send the person back through the hand-off for a key mismatch.
    assert resp.status_code == 500 and resp.json() == {"error": "Something went wrong on our side. Please try again."}
