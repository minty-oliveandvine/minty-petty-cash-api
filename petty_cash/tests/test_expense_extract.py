"""Reading a receipt with the model: ``POST /reports/expenses/extract`` (``petty_cash/routes/
expenses.py``). This service holds no model code, so after checking who asks and the file it
forwards the receipt to Flask's ``POST /api/internal/petty-cash/expense-extract``
(``core/accounts_api.py::forward_expense_extract``), whose ``services/expense_ai.py`` is the only
module that talks to the model; Flask's answer comes back unchanged. HTTP to Flask is faked
(Flask's side is Minty's ``tests/test_expense_ai_routes.py``)."""

from __future__ import annotations

import jwt
import pytest
import requests
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from minty_db.enums import EntityRole
from minty_db.models import UserEntity

from petty_cash.tests.conftest import WEB, headers, make_user, url
from petty_cash.tests.test_deposit_correction import _Response
from petty_cash.tests.test_expenses import JPEG, PNG

pytestmark = pytest.mark.django_db
EXTRACT = "http://flask.invalid/api/internal/petty-cash/expense-extract"
FIELD = {"value": "ABC Supplies", "confidence": 0.93, "band": "high", "applied": True, "contact_id": "con-0001"}
READ = {"suggestions": {"supplier": FIELD, "currency": "HKD", "entity_currency": "HKD"}, "reason": None}


class FakeFlask:
    def __init__(self):
        self.sent = []
        self.answer = _Response(200, READ)

    def post(self, url, json=None, files=None, headers=None, timeout=None, **_):
        self.sent.append({"url": url, "json": json, "files": files, "headers": headers or {}, "timeout": timeout})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def flask(monkeypatch) -> FakeFlask:
    fake = FakeFlask()
    monkeypatch.setattr("requests.post", fake.post)
    return fake


def _extract(client, shop, user=None, *, receipt=PNG, name="slip.jpg", files=None):
    form = {}
    if files is not None:
        form["receipt"] = files
    elif receipt is not None:
        form["receipt"] = SimpleUploadedFile(name, receipt, content_type="image/jpeg")
    return client.post(url(shop.entity.id, "/reports/expenses/extract"), form,
                       headers=headers((user or shop.owner).id))


def test_the_receipt_goes_to_flask_and_its_reading_comes_back(client, shop, flask):
    resp = _extract(client, shop)

    assert resp.status_code == 200 and resp.json() == READ
    sent, = flask.sent
    assert sent["url"] == EXTRACT and sent["json"] is None
    # the bytes as sent, typed by the bytes (a PNG named .jpg and sent as image/jpeg)
    assert sent["files"] == {"receipt": ("slip.jpg", PNG, "image/png")}
    assert sent["timeout"][1] <= 60
    assert resp["Access-Control-Allow-Origin"] == WEB and resp["Cache-Control"] == "no-store"


def test_the_assertion_names_this_company_and_this_person(client, shop, flask):
    _extract(client, shop)

    assertion = flask.sent[0]["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(assertion, settings.SECRET_KEY, algorithms=["HS256"])
    assert claims["scope"] == "petty-cash-expense-extract"
    assert (claims["entity_id"], claims["user_id"]) == (str(shop.entity.id), str(shop.owner.id))
    assert claims["exp"] - claims["iat"] == 60


@pytest.mark.parametrize("status, body", [
    (200, {"suggestions": None, "reason": "disabled"}),
    (200, {"suggestions": None, "reason": "rate_limited"}),
    (200, {"suggestions": None, "reason": "no_context"}),
    (403, {"error": "You do not have permission to edit reports for this entity."}),
    (422, {"error": "Attach a PDF, JPEG or PNG file."}),
])
def test_flask_s_answers_are_handed_on_unchanged(client, shop, flask, status, body):
    flask.answer = _Response(status, body)

    resp = _extract(client, shop)

    assert resp.status_code == status and resp.json() == body


@pytest.mark.parametrize("answer, reason", [
    (requests.Timeout("slow"), "timeout"),
    (requests.ConnectionError("down"), "provider_error"),
    (_Response(502, None, text="<html>Bad gateway</html>"), "provider_error"),
    (_Response(500, {"message": "not the API's shape"}), "provider_error"),
    (_Response(200, {"unexpected": True}), "provider_error"),
], ids=["timeout", "unreachable", "html", "no-error-key", "no-suggestions-key"])
def test_no_usable_answer_is_a_silent_no_suggestion(client, shop, flask, answer, reason):
    flask.answer = answer

    resp = _extract(client, shop)

    assert resp.status_code == 200 and resp.json() == {"suggestions": None, "reason": reason}


def test_flask_refusing_our_assertion_is_a_500_not_a_401(client, shop, flask):
    flask.answer = _Response(401, {"error": "unauthorized"})

    resp = _extract(client, shop)

    assert resp.status_code == 500
    assert resp.json() == {"error": "Something went wrong on our side. Please try again."}


@pytest.mark.parametrize("receipt, message", [
    (None, "Attach the receipt to read it."),
    (b"GIF89a-not-allowed", "Attach a PDF, JPEG or PNG file."),
    (b"", "That file is empty. Attach the receipt again."),
    (b"%PDF" + b"0" * (10 * 1024 * 1024), "Attach a file of 10 MB or less."),
], ids=["missing", "gif", "empty", "too-big"])
def test_a_file_that_is_not_a_receipt_is_refused_here(client, shop, flask, receipt, message):
    resp = _extract(client, shop, receipt=receipt)

    assert resp.status_code == 422 and resp.json() == {"error": message}
    assert flask.sent == []


def test_two_files_are_refused_here(client, shop, flask):
    files = [SimpleUploadedFile("a.jpg", JPEG), SimpleUploadedFile("b.jpg", JPEG)]

    resp = _extract(client, shop, files=files)

    assert resp.status_code == 422 and resp.json() == {"error": "Attach one receipt at a time."}
    assert flask.sent == []


def test_who_may_ask_is_decided_here(client, shop, flask):
    base = make_user("base@test.com")
    UserEntity.objects.create(user_id=base.id, entity_id=shop.entity.id, role=EntityRole.ENTITY_BASE, approved=True)
    stranger = make_user("stranger@test.com")

    refused = _extract(client, shop, user=base)
    assert refused.status_code == 403
    assert refused.json() == {"error": "You do not have permission to edit reports for this entity."}
    assert _extract(client, shop, user=stranger).status_code == 403  # the door: not a member
    no_token = client.post(url(shop.entity.id, "/reports/expenses/extract"),
                           {"receipt": SimpleUploadedFile("a.jpg", JPEG)})
    assert no_token.status_code == 401
    assert flask.sent == []
