"""Report history (``petty_cash/routes/history.py``) - Flask's ``tests/test_petty_cash_api_history.py``
and ``tests/test_report_history_state.py`` expectations, restated. A failed report's reasons come
from Flask's internal route (faked here; Flask's side is Minty's
``tests/test_petty_cash_internal_publish_reasons.py``)."""

from __future__ import annotations

import json
from datetime import timedelta

import jwt
import pytest
import requests
from django.conf import settings

from petty_cash.tests.conftest import headers, make_entity, make_report, make_user, url
from petty_cash.tests.test_close import D3
from shared_models.enums import EntityRole
from shared_models.models import UserEntity

pytestmark = pytest.mark.django_db
FLASK = "http://flask.invalid/api/internal/xero/publish-reasons"
REASON = {"text": "No account code for Cleaning", "resolved": False}


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeFlask:
    def __init__(self):
        self.sent = []
        self.failure = None

    def post(self, url, json=None, headers=None, timeout=None, **_):
        self.sent.append({"url": url, "json": json, "headers": headers or {}})
        if self.failure is not None:
            if isinstance(self.failure, Exception):
                raise self.failure
            return self.failure
        return _Response(200, {"reasons": {i: [REASON] for i in json["report_ids"]}})


@pytest.fixture
def flask(monkeypatch) -> FakeFlask:
    fake = FakeFlask()
    monkeypatch.setattr("requests.post", fake.post)
    return fake


def _older(shop, days_back=1, **fields):
    values = {"opening_balance": 0, "closing_balance": 10, "completed_sections": ["opening"],
              "current_section": "submitted"}
    values.update(fields)
    return make_report(shop.entity, D3 - timedelta(days=days_back), status=values.pop("status", "submitted"),
                       created_by=shop.owner.id, **values)


def _get(client, shop, user=None, **query):
    return client.get(url(shop.entity.id, "/reports"), query, headers=headers((user or shop.owner).id))


def test_one_page_newest_first_with_what_may_be_done(client, shop, flask):
    older = _older(shop, days_back=1, publishing_status="failed")
    latest = _older(shop, days_back=0, total_sales=12, expense_total=3)

    resp = _get(client, shop)

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert [r["id"] for r in body["reports"]] == [str(latest.id), str(older.id)]
    first, second = body["reports"]
    assert first["transaction_date"] == D3.isoformat()
    assert (first["status"], first["status_label"], first["status_tone"]) == ("submitted", "Submitted", "info")
    assert first["submitted_by"] == "Olive" and first["publish_reasons"] == []
    assert (first["total_sales"], first["total_expenses"]) == (12, 3)
    assert first["created_at"].endswith("+08:00") and first["submitted_at"] is None
    assert first["was_published"] is False and second["was_published"] is True
    assert (first["publishing_status"], second["publishing_status"]) == ("unpublished", "failed")
    assert first["permissions"] == {"edit": True, "delete": True}
    assert second["status_label"] == "Publish failed" and second["permissions"] == {"edit": False, "delete": False}
    assert second["publish_reasons"] == [REASON]
    assert {k: body[k] for k in ("page", "per_page", "total_pages", "total_count", "latest_report_id",
                                 "can_publish", "currency_code")} == {
        "page": 1, "per_page": 10, "total_pages": 1, "total_count": 2, "latest_report_id": str(latest.id),
        "can_publish": True, "currency_code": "HKD"}


def test_reasons_are_asked_of_flask_for_the_failed_rows_on_the_page_only(client, shop, flask):
    failed = _older(shop, days_back=1, publishing_status="failed")
    _older(shop, days_back=0)

    _get(client, shop)

    sent, = flask.sent
    assert sent["url"] == FLASK and sent["json"] == {"report_ids": [str(failed.id)]}
    assertion = sent["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(assertion, settings.SECRET_KEY, algorithms=["HS256"])
    assert (claims["scope"], claims["entity_id"]) == ("petty-cash-publish-reasons", str(shop.entity.id))


def test_a_page_with_no_failed_report_never_asks_flask(client, shop, flask):
    _older(shop, days_back=0)

    assert _get(client, shop).status_code == 200 and flask.sent == []


@pytest.mark.parametrize("failure", [requests.ConnectionError("down"), _Response(500, {"error": "x"}),
                                     _Response(200, {"nope": 1})])
def test_reasons_flask_cannot_give_make_the_page_fail_rather_than_show_none(client, shop, flask, failure):
    _older(shop, days_back=0, publishing_status="failed")
    flask.failure = failure

    resp = _get(client, shop)

    assert resp.status_code == 503
    assert resp.json() == {"error": "I couldn't load why some reports didn't publish. Try again in a moment."}


def test_filters_and_pages(client, shop, flask):
    ids = [str(_older(shop, days_back=i).id) for i in range(12)]
    draft = _older(shop, days_back=20, status="draft")

    page2 = _get(client, shop, page=2).json()
    assert [r["id"] for r in page2["reports"]] == ids[10:] + [str(draft.id)] and page2["total_pages"] == 2
    drafts = _get(client, shop, status="draft").json()["reports"]
    assert [(r["id"], r["current_section"]) for r in drafts] == [(str(draft.id), "submitted")]
    assert drafts[0]["created_at"] and drafts[0]["submitted_at"] is None
    ranged = _get(client, shop, start_date=(D3 - timedelta(days=2)).isoformat(), end_date=D3.isoformat()).json()
    assert [r["id"] for r in ranged["reports"]] == ids[:3]
    assert _get(client, shop, per_page=25).json()["total_pages"] == 1


def test_each_status_filter_keeps_its_pill(client, shop, flask):
    rows = {
        "draft": _older(shop, days_back=0, status="draft"),
        "submitted": _older(shop, days_back=1),
        "published": _older(shop, days_back=2, xero_integrated=True, publishing_status="completed"),
        "failed": _older(shop, days_back=3, publishing_status="failed"),
        "publishing": _older(shop, days_back=4, publishing_status="publishing"),
        "edited after xero": _older(shop, days_back=5, xero_integrated=False, publishing_status="completed"),
    }

    def ids(status):
        return {r["id"] for r in _get(client, shop, status=status).json()["reports"]}

    assert ids("draft") == {str(rows["draft"].id)}
    assert ids("published") == {str(rows["published"].id)}
    assert ids("failed") == {str(rows["failed"].id), str(rows["publishing"].id)}
    assert ids("submitted") == {str(rows["submitted"].id), str(rows["edited after xero"].id)}


@pytest.mark.parametrize("query, message", [
    ({"status": "nonsense"}, "That status filter isn't one I know. Choose Draft, Submitted, Published or Failed."),
    ({"per_page": 7}, "Show 10, 25, 50 or 100 reports a page."),
    ({"page": 0}, "Pages start at 1. Choose a page from the list."),
    ({"page": "x"}, "The page or the page size isn't a whole number. Choose a page from the list."),
    ({"start_date": "2026-10-01"}, "Choose both dates to see the reports between them."),
    ({"start_date": "01/10/2026", "end_date": "2026-10-02"}, "A date isn't in the YYYY-MM-DD form. Choose it from the calendar."),
    ({"start_date": "2026-10-05", "end_date": "2026-10-02"},
     "The start date is after the end date. Choose a start on or before the end."),
])
def test_a_bad_filter_is_refused(client, shop, flask, query, message):
    resp = _get(client, shop, **query)

    assert resp.status_code == 422 and resp.json() == {"error": message}


def test_a_cashier_may_edit_but_not_delete_someone_elses_latest_report(client, shop, flask):
    _older(shop, days_back=0)
    cashier = make_user("cashier@test.com")
    UserEntity.objects.create(user_id=cashier.id, entity_id=shop.entity.id, role=EntityRole.CASHIER, approved=True)

    row = _get(client, shop, user=cashier).json()["reports"][0]

    assert row["permissions"] == {"edit": True, "delete": False}


def test_an_empty_history_is_one_empty_page(client, shop, flask):
    body = _get(client, shop).json()

    assert body["reports"] == [] and (body["total_pages"], body["total_count"]) == (1, 0)
    assert body["latest_report_id"] is None


def test_another_companys_history_is_refused_at_the_door(client, shop, flask):
    theirs = make_entity(make_user("stranger@test.com"), name="Their Shop")

    resp = client.get(url(theirs.id, "/reports"), headers=headers(shop.owner.id))

    assert resp.status_code == 403


def test_a_stale_draft_in_the_history_may_be_deleted(client, shop, flask):
    """A draft dated before the last submitted day: never the latest, yet deletable (2026-10-26)."""
    _older(shop, days_back=0)
    stale = _older(shop, days_back=4, status="draft", current_section="sales")

    rows = {r["id"]: r for r in _get(client, shop).json()["reports"]}

    assert rows[str(stale.id)]["permissions"] == {"edit": False, "delete": True}
