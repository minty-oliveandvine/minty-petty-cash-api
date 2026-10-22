"""The one log line (minty_shared.logctx) as this service writes it: a trace per request, taken
from a well-formed ``X-Request-ID`` and echoed back; the person bound once the token is good, the
company once every door check has passed; the route name as the fallback step; and the trace sent
on every call to Flask, never to Xero."""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace

import pytest
from minty_shared import logctx

from petty_cash.tests.conftest import headers, make_entity, make_user, url

pytestmark = pytest.mark.django_db

HEX8 = re.compile(r"[0-9a-f]{8}")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.addFilter(logctx.ContextFilter())
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def lines():
    capture = _Capture()
    loggers = [logging.getLogger("petty-cash-api"), logging.getLogger("petty-cash-api.http")]
    for lg in loggers:
        lg.addHandler(capture)
    yield capture.records
    for lg in loggers:
        lg.removeHandler(capture)


def _access(records):
    (record,) = [r for r in records if r.name == "petty-cash-api.http"]
    return record


def test_home_carries_trace_person_company_and_route(client, shop, lines):
    resp = client.get(
        url(shop.entity.id, "/home"), headers={**headers(shop.owner.id), "X-Request-ID": "deadbeef"}
    )
    assert resp.status_code == 200
    assert resp["X-Request-ID"] == "deadbeef"
    record = _access(lines)
    assert (record.trace, record.user, record.entity) == (
        "deadbeef",
        str(shop.owner.id),
        str(shop.entity.id),
    )
    assert record.step == "home"
    assert re.fullmatch(rf"GET {re.escape(url(shop.entity.id, '/home'))} 200 \d+ms", record.getMessage())


@pytest.mark.parametrize("incoming", [None, "short", "DEADBEEF", "deadbeef\nINFO forged"])
def test_a_missing_or_malformed_trace_is_replaced(client, shop, lines, incoming):
    extra = {"X-Request-ID": incoming} if incoming is not None else {}
    resp = client.get(url(shop.entity.id, "/home"), headers={**headers(shop.owner.id), **extra})
    assert HEX8.fullmatch(resp["X-Request-ID"])
    assert _access(lines).trace == resp["X-Request-ID"]


def test_a_company_the_person_cannot_open_is_never_bound(client, shop, lines):
    stranger = make_user("stranger@test.com")
    resp = client.get(url(shop.entity.id, "/home"), headers=headers(stranger.id))
    assert resp.status_code == 403
    record = _access(lines)
    assert (record.user, record.entity) == (str(stranger.id), "-")


def test_a_bad_token_names_nobody(client, shop, lines):
    resp = client.get(url(shop.entity.id, "/home"), headers=headers(shop.owner.id, expires_in=-60))
    assert resp.status_code == 401
    assert (_access(lines).user, _access(lines).entity) == ("-", "-")


def test_nothing_outlives_the_request(client, shop):
    client.get(url(shop.entity.id, "/home"), headers=headers(shop.owner.id))
    assert logctx.current() == {"trace": "-", "user": "-", "entity": "-", "step": "-"}


def test_calls_to_flask_carry_the_trace_and_xero_calls_do_not(monkeypatch, settings, shop):
    import requests

    from core import accounts_api, xero_tokens

    settings.ACCOUNTS_API_URL = "http://flask.test"
    sent = []

    def fake_post(url, headers=None, **kwargs):
        sent.append((url, dict(headers or {})))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"access_token": "t", "xero_org_id": "o", "reasons": {}},
            text="",
        )

    monkeypatch.setattr(requests, "post", fake_post)
    logctx.start_trace("a1b2c3d4")
    try:
        xero_tokens.xero_token(shop.entity.id)
        accounts_api.publish_reasons(shop.entity.id, [])
    finally:
        logctx.reset()
    assert [h.get("X-Request-ID") for _, h in sent] == ["a1b2c3d4", "a1b2c3d4"]
    assert all(u.startswith("http://flask.test/") for u, _ in sent)


def test_the_supplier_call_to_xero_sends_no_trace():
    from pathlib import Path

    source = Path(__file__).resolve().parents[1].joinpath("services", "suppliers.py").read_text(
        encoding="utf-8"
    )
    assert "outgoing_headers" not in source and "assertion_headers" not in source


def test_a_second_company_is_bound_as_itself(client, shop, lines):
    other = make_entity(shop.owner, name="Second Shop")
    client.get(url(other.id, "/home"), headers=headers(shop.owner.id))
    assert _access(lines).entity == str(other.id)
