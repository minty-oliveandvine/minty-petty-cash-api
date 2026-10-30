"""``POST /suppliers`` (``services/suppliers.py``) - Flask's ``tests/test_supplier_create.py`` API
expectations, restated, with the access token from Flask's token service (``core/xero_tokens.py``)
instead of Flask's own credentials. HTTP is faked: the token service and Xero's Contacts."""

from __future__ import annotations

import json

import jwt
import pytest
from django.conf import settings
from minty_db.enums import EntityRole
from minty_db.models import Entity, UserEntity, XeroContactSync

from petty_cash.tests.conftest import headers, make_user, url

pytestmark = pytest.mark.django_db
ORG = "org-1234"
TOKEN_URL = "http://flask.invalid/api/internal/xero/token"
CONTACTS_URL = "https://api.xero.com/api.xro/2.0/Contacts"


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeHttp:
    """Answers the token service and Xero; records what was sent."""

    def __init__(self):
        self.sent = []
        self.token_status = 200
        self.xero_status = 200
        self.contacts = [{"ContactID": "con-new", "Name": "Corner Bakery"}]

    def post(self, url, headers=None, json=None, timeout=None, **_):
        self.sent.append({"url": url, "headers": headers or {}, "json": json})
        if url == TOKEN_URL:
            if self.token_status != 200:
                return _Response(self.token_status, {"status": "reconnect_required"})
            return _Response(200, {"access_token": "xero-access", "xero_org_id": ORG})
        if url == CONTACTS_URL:
            return _Response(self.xero_status, {"Contacts": self.contacts} if self.xero_status == 200 else {})
        raise AssertionError(f"unexpected POST {url}")


@pytest.fixture
def http(monkeypatch) -> FakeHttp:
    fake = FakeHttp()
    monkeypatch.setattr("requests.post", fake.post)
    return fake


@pytest.fixture
def connected(shop):
    Entity.objects.filter(id=shop.entity.id).update(xero_org_id=ORG)
    return shop


def _post(client, shop, body, user=None):
    return client.post(url(shop.entity.id, "/suppliers"), body, content_type="application/json",
                       headers=headers((user or shop.owner).id))


def _synced(shop) -> dict:
    return dict(XeroContactSync.objects.filter(entity_id=shop.entity.id).values_list("xero_contact_id", "name"))


def test_a_new_supplier_is_created_in_xero_and_kept_as_a_synced_contact(client, connected, http):
    resp = _post(client, connected, {"name": " Corner Bakery "})

    assert resp.status_code == 201 and resp.json() == {"id": "con-new", "name": "Corner Bakery"}
    token_call, xero_call = http.sent
    assert xero_call["url"] == CONTACTS_URL
    assert xero_call["headers"]["Authorization"] == "Bearer xero-access"
    assert xero_call["headers"]["Xero-Tenant-Id"] == ORG
    assert xero_call["json"] == {"Contacts": [{"Name": "Corner Bakery", "IsCustomer": True, "IsSupplier": False}]}
    assert _synced(connected) == {"con-new": "Corner Bakery"}
    assert XeroContactSync.objects.get(xero_contact_id="con-new").xero_org_id == ORG


def test_the_token_is_asked_of_flask_for_this_company_only(client, connected, http):
    _post(client, connected, {"name": "Corner Bakery"})

    assertion = http.sent[0]["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(assertion, settings.SECRET_KEY, algorithms=["HS256"])
    assert claims["scope"] == "xero-access-token" and claims["entity_id"] == str(connected.entity.id)
    assert claims["exp"] - claims["iat"] == 60


def test_an_existing_synced_contact_is_renamed_not_duplicated(client, connected, http):
    XeroContactSync.objects.create(entity_id=connected.entity.id, xero_contact_id="con-new", name="Old name")

    _post(client, connected, {"name": "Corner Bakery"})

    assert _synced(connected) == {"con-new": "Corner Bakery"}


@pytest.mark.parametrize("body, message", [
    ({"name": "  "}, "The supplier's name is still empty. Add the name to continue."),
    ({}, "The supplier's name is still empty. Add the name to continue."),
    ({"name": 7}, "The supplier's name is still empty. Add the name to continue."),
    ({"name": "x" * 151}, "Keep the supplier's name to 150 characters or fewer."),
])
def test_a_bad_name_is_refused_before_calling_xero(client, connected, http, body, message):
    resp = _post(client, connected, body)

    assert resp.status_code == 422 and resp.json()["error"] == message
    assert http.sent == []


def test_xero_refusing_is_said_and_nothing_is_kept(client, connected, http):
    http.xero_status = 500

    resp = _post(client, connected, {"name": "Corner Bakery"})

    assert resp.status_code == 409
    assert resp.json() == {"error": "I couldn't add that contact to Xero. Mind trying again?"}
    assert _synced(connected) == {}


def test_a_contact_without_an_id_is_said(client, connected, http):
    http.contacts = [{"Name": "Corner Bakery"}]

    resp = _post(client, connected, {"name": "Corner Bakery"})

    assert resp.status_code == 409 and resp.json()["error"].startswith("Xero didn't tell me which contact")
    assert _synced(connected) == {}


def test_a_company_without_xero_cannot_add_one(client, shop, http):
    resp = _post(client, shop, {"name": "Corner Bakery"})

    assert resp.status_code == 409
    assert resp.json()["error"] == "This entity isn't connected to Xero yet. Could you ask an admin to connect it?"
    assert http.sent == []


def test_a_connection_needing_a_reconnect_is_said(client, connected, http):
    http.token_status = 409

    resp = _post(client, connected, {"name": "Corner Bakery"})

    assert resp.status_code == 409
    assert resp.json()["error"] == "Xero wouldn't let me in. Could you ask an admin to reconnect it?"
    assert [s["url"] for s in http.sent] == [TOKEN_URL]


def test_a_token_service_failure_is_a_503(client, connected, http):
    http.token_status = 500

    resp = _post(client, connected, {"name": "Corner Bakery"})

    assert resp.status_code == 503 and _synced(connected) == {}


def test_xero_refusing_the_token_asks_for_a_reconnect(client, connected, http):
    http.xero_status = 401

    resp = _post(client, connected, {"name": "Corner Bakery"})

    assert resp.status_code == 409 and resp.json()["error"].startswith("Xero wouldn't let me in")


def test_a_member_without_the_right_is_refused(client, connected, http):
    base = make_user("base@test.com")
    UserEntity.objects.create(user_id=base.id, entity_id=connected.entity.id, role=EntityRole.ENTITY_BASE,
                              approved=True)

    resp = _post(client, connected, {"name": "Corner Bakery"}, user=base)

    assert resp.status_code == 403 and http.sent == []
