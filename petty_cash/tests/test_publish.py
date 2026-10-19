"""Publishing to Xero (``petty_cash/routes/report.py``): ``POST /reports/{id}/publish`` is done by
Flask's internal route (``core/accounts_api.py::forward_publish``, the owner's choice: this service
holds no Xero code) after this service checks the report and ``REPORT_PUBLISH``;
``GET /reports/{id}/publishing-status`` is read here, a failed publish's reasons asked of Flask.
Flask's ``tests/test_petty_cash_api_report.py`` (publishing section) and
``tests/test_report_publish_state.py`` expectations, restated; HTTP to Flask is faked (Flask's side
is Minty's ``tests/test_petty_cash_internal_publish.py``)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import jwt
import pytest
import requests
from django.conf import settings

from core.accounts_api import PUBLISH_UNCONFIRMED
from petty_cash.routes.history import NO_REASONS
from petty_cash.tests.conftest import headers, make_entity, make_report, make_user, url
from petty_cash.tests.test_close import D3
from petty_cash.tests.test_deposit_correction import _Response
from petty_cash.tests.test_report import _older, _set
from shared_models.enums import EntityRole
from shared_models.models import UserEntity

pytestmark = pytest.mark.django_db
PUBLISH = "http://flask.invalid/api/internal/xero/publish"
REASONS = "http://flask.invalid/api/internal/xero/publish-reasons"
STARTED = {"publishing_status": "publishing", "message": "Publishing to Xero. This can take a few minutes."}
REASON = {"text": "No account code for Fees", "resolved": True}


class FakeFlask:
    def __init__(self):
        self.sent = []
        self.answer = _Response(202, STARTED)

    def post(self, url, json=None, headers=None, timeout=None, **_):
        self.sent.append({"url": url, "json": json, "headers": headers or {}, "timeout": timeout})
        if isinstance(self.answer, Exception):
            raise self.answer
        if url == REASONS and self.answer is None:
            return _Response(200, {"reasons": {i: [{**REASON, "resolvable": True}] for i in json["report_ids"]}})
        return self.answer


@pytest.fixture
def flask(monkeypatch) -> FakeFlask:
    fake = FakeFlask()
    monkeypatch.setattr("requests.post", fake.post)
    return fake


def _publish(client, shop, report_id, user=None):
    return client.post(url(shop.entity.id, f"/reports/{report_id}/publish"), headers=headers((user or shop.owner).id))


def _status(client, shop, report_id):
    return client.get(url(shop.entity.id, f"/reports/{report_id}/publishing-status"), headers=headers(shop.owner.id))


# --- POST /reports/{id}/publish -------------------------------------------------------------------


def test_the_publish_goes_to_flask_and_its_202_comes_back(client, shop, flask):
    report = _older(shop)

    resp = _publish(client, shop, report.id)

    assert resp.status_code == 202 and resp.json() == STARTED
    sent, = flask.sent
    assert sent["url"] == PUBLISH and sent["json"] == {"report_id": str(report.id)}
    assert sent["timeout"][1] <= 60  # the start is quick; the worker runs after the 202


def test_the_assertion_names_this_company_and_this_person(client, shop, flask):
    _publish(client, shop, _older(shop).id)

    assertion = flask.sent[0]["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(assertion, settings.SECRET_KEY, algorithms=["HS256"])
    assert claims["scope"] == "petty-cash-publish"
    assert (claims["entity_id"], claims["user_id"]) == (str(shop.entity.id), str(shop.owner.id))
    assert claims["exp"] - claims["iat"] == 60


@pytest.mark.parametrize("status, body", [
    (400, {"error": "I've lost access to your Xero account. Mind reconnecting it in Settings?", "reconnect": True}),
    (400, {"error": "Xero settings incomplete. Please configure entity settings first."}),
    (409, {"error": "Publishing already in progress. Please wait..."}),
    (409, {"error": "This report is still a draft. Submit the day before publishing it."}),
])
def test_flasks_refusals_come_back_as_they_are(client, shop, flask, status, body):
    flask.answer = _Response(status, body)

    resp = _publish(client, shop, _older(shop).id)

    assert resp.status_code == status and resp.json() == body


def test_an_unknown_or_another_companys_report_is_404_and_flask_is_never_asked(client, shop, flask):
    elsewhere = make_entity(make_user("other@test.com"), name="Elsewhere")
    foreign = make_report(elsewhere, D3, created_by=shop.owner.id)

    for report_id in (uuid.uuid4(), "nope", foreign.id):
        resp = _publish(client, shop, report_id)
        assert resp.status_code == 404 and resp.json() == {"error": "Hmm, I couldn't find that report."}
    assert flask.sent == []


def test_a_person_without_the_publish_right_is_403_and_flask_is_never_asked(client, shop, flask):
    cashier = make_user("cashier@test.com")
    UserEntity.objects.create(user_id=cashier.id, entity_id=shop.entity.id, role=EntityRole.CASHIER, approved=True)
    report = make_report(shop.entity, D3, created_by=cashier.id)  # their own report, which they may see

    resp = _publish(client, shop, report.id, user=cashier)

    assert resp.status_code == 403
    assert resp.json() == {"error": "You don't have permission to publish reports for this company."}
    assert flask.sent == []


@pytest.mark.parametrize("failure, status", [
    (requests.ConnectionError("refused"), 503),
    (requests.ReadTimeout("slow"), 504),
    (_Response(502, text="<html>Bad gateway</html>"), 503),
    (_Response(500, {"status": "error"}), 503),
])
def test_no_usable_answer_says_to_check_the_status(client, shop, flask, failure, status):
    """The publish may have started: never claim it did not."""
    flask.answer = failure

    resp = _publish(client, shop, _older(shop).id)

    assert resp.status_code == status and resp.json() == {"error": PUBLISH_UNCONFIRMED}


def test_flask_refusing_this_services_assertion_is_our_fault_not_the_persons(client, shop, flask):
    flask.answer = _Response(401, {"error": "unauthorized"})

    resp = _publish(client, shop, _older(shop).id)

    assert resp.status_code == 500 and resp.json() == {"error": "Something went wrong on our side. Please try again."}


# --- GET /reports/{id}/publishing-status ----------------------------------------------------------


def test_a_publishing_report_says_so_and_flask_is_never_asked(client, shop, flask):
    report = _older(shop)
    _set(report, publishing_status="publishing")

    resp = _status(client, shop, report.id)

    assert resp.status_code == 200, resp.json()
    assert resp.json() == {
        "report_id": str(report.id), "publishing_status": "publishing", "status_label": "Publishing to Xero",
        "status_tone": "info", "xero_integrated": False, "was_published": True, "published_at": None, "reasons": [],
    }
    assert flask.sent == []


def test_a_failed_publish_brings_its_reasons_from_flask(client, shop, flask):
    report = _older(shop)
    _set(report, publishing_status="failed")
    flask.answer = None  # the reasons route answers

    body = _status(client, shop, report.id).json()

    assert (body["publishing_status"], body["status_label"], body["status_tone"]) == ("failed", "Publish failed", "danger")
    assert body["reasons"] == [REASON]
    sent, = flask.sent
    assert sent["url"] == REASONS and sent["json"] == {"report_ids": [str(report.id)]}


def test_failed_reasons_flask_cannot_give_make_a_503(client, shop, flask):
    report = _older(shop)
    _set(report, publishing_status="failed")
    flask.answer = requests.ConnectionError("down")

    resp = _status(client, shop, report.id)

    assert resp.status_code == 503 and resp.json() == {"error": NO_REASONS}


def test_a_report_in_xero_reads_published(client, shop, flask):
    report = _older(shop)
    _set(report, status="published", publishing_status="completed", xero_integrated=True,
         published_at=datetime(2026, 10, 14, 2, 30, tzinfo=UTC))

    body = _status(client, shop, report.id).json()

    assert body == {
        "report_id": str(report.id), "publishing_status": "completed", "status_label": "Published to Xero",
        "status_tone": "success", "xero_integrated": True, "was_published": True,
        "published_at": "2026-10-14T10:30:00+08:00", "reasons": [],
    }
    assert flask.sent == []


def test_a_submitted_report_never_published(client, shop, flask):
    body = _status(client, shop, (report := _older(shop)).id).json()

    assert (body["report_id"], body["status_label"], body["was_published"], body["reasons"]) == (
        str(report.id), "Submitted", False, [])


def test_another_companys_report_status_is_404(client, shop, flask):
    assert _status(client, shop, uuid.uuid4()).status_code == 404
