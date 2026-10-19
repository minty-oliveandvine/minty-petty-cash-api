"""The public share read (``petty_cash/routes/public_share.py``):
``GET /api/petty-cash/v1/public/share/{initials}/{date}/{secret}`` - Flask's
``tests/test_petty_cash_api_public_share.py`` expectations, restated. The ONE operation with no
token: these tests pin that it is the only one, that it answers no more than Flask's shared page
shows (no ids, permissions, options or receipt access), that every refusal reads the same except an
expired link (410, row deleted), and that the secret never reaches a log."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

import pytest
from django.conf import settings
from django.test import Client

from petty_cash.services.share import share_token, verify_share_token
from petty_cash.tests.conftest import WEB, headers, url
from petty_cash.tests.test_close import D3, _ready, _submit
from shared_models.models import (
    Attachment,
    Report,
    ReportExpense,
    ReportExpenseAttachment,
    ShareLink,
)

pytestmark = pytest.mark.django_db

PUBLIC = "/api/petty-cash/v1/public/share/"
GONE = {"error": "This link doesn't work. Ask the person who shared it for a new link."}
EXPIRED = {"error": "This link has expired. Ask the person who shared it for a new link."}


@pytest.fixture
def shared(client, shop, notes):
    report = _ready(shop, notes)
    assert _submit(client, shop).status_code == 200
    resp = client.post(url(shop.entity.id, f"/reports/{report.id}/share-link"), headers=headers(shop.owner.id))
    assert resp.status_code == 200, resp.json()
    return report, resp.json()


def _read(path, origin=WEB):
    return Client().get(PUBLIC + path, headers={"Origin": origin})  # no token, no session


def _keys(value, prefix=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield f"{prefix}{k}"
            yield from _keys(v, f"{prefix}{k}.")
    elif isinstance(value, list):
        for v in value:
            yield from _keys(v, prefix)


# --- the share-link answer and the token -----------------------------------------------------------


def test_the_share_link_answers_the_path_the_apps_build_their_own_page_from(shared):
    _report, body = shared

    assert set(body) == {"url", "path", "expires_at"}
    assert re.fullmatch(r"[A-Za-z]+/\d{2}_[A-Z][a-z]{2}_\d{4}/[A-Za-z0-9_-]{32}", body["path"])
    assert body["url"] == f"{settings.ACCOUNTS_API_URL}/Minty_Report/{body['path']}/"
    assert timedelta(days=29) < datetime.fromisoformat(body["expires_at"]) - datetime.now(UTC) <= timedelta(days=30)


def test_verify_share_token_is_flasks(shop):
    token = share_token(shop.entity.id, D3, settings.SECRET_KEY)
    assert verify_share_token(token, settings.SECRET_KEY) == (
        True, {"entity_id": str(shop.entity.id), "transaction_date": D3.isoformat()})
    assert verify_share_token(token, "another key") == (False, None)
    assert verify_share_token("not base64 at all!", settings.SECRET_KEY) == (False, None)
    assert verify_share_token(share_token(shop.entity.id, D3, settings.SECRET_KEY, expiration_hours=-1),
                              settings.SECRET_KEY) == (False, None)


# --- the read --------------------------------------------------------------------------------------


def test_anyone_with_the_link_reads_the_day_and_nothing_more(shop, shared):
    report, body = shared
    line = ReportExpense.objects.get(report_id=report.id)
    receipt = Attachment.objects.create(file_path=f"{shop.entity.id}/receipts/lunch.jpg", original_name="lunch.jpg",
                                        mime_type="image/jpeg")
    ReportExpenseAttachment.objects.create(report_expense_id=line.id, attachment_id=receipt.id, sort_order=0,
                                           xero_attachment_id="")

    resp = _read(body["path"])

    assert resp.status_code == 200, resp.json()
    day = resp.json()
    assert set(day) == {
        "company_name", "transaction_date", "currency_code", "status", "status_label", "status_tone",
        "publishing_status", "submitted_at", "submitted_by", "kpis", "sales", "expenses", "cash",
        "closing_balance", "expires_at",
    }
    assert day["company_name"] == "Acme Shop" and day["transaction_date"] == D3.isoformat()
    assert (day["status"], day["status_label"], day["status_tone"]) == ("submitted", "Submitted", "info")
    assert day["submitted_by"] == "Olive Owner" and day["submitted_at"]
    assert day["kpis"] == {"total_sales": 0, "total_expenses": 15, "cash_position": 55}
    assert set(day["sales"]) == {"methods", "cash", "total"}
    assert day["expenses"] == {
        "lines": [{"item": "Advertising", "supplier_name": None, "amount": 15, "receipts": [{"name": "lunch.jpg"}]}],
        "total": 15,
    }
    assert day["cash"]["difference"] == {"amount": 0, "kind": "none", "reason": ""}
    assert day["cash"]["expected"] == 55 and day["closing_balance"] == 55
    assert day["expires_at"] == body["expires_at"]
    keys = set(_keys(day))
    assert not [k for k in keys if k.split(".")[-1] in {"id", "sale_id", "report_id", "key", "mime_type"}
                or k.endswith("_id") or k.split(".")[0] in {"permissions", "options"}], keys
    assert str(shop.entity.id) not in resp.content.decode()


def test_the_answer_is_never_cached_or_leaked_by_referrer(shared):
    path = shared[1]["path"]

    for resp in (_read(path), _read("AB/01_Jan_2026/" + "B" * 32)):
        assert resp["Cache-Control"] == "no-store"
        assert resp["Referrer-Policy"] == "no-referrer"
        assert resp["Access-Control-Allow-Origin"] == WEB
        assert "Access-Control-Allow-Credentials" not in resp
    assert "Access-Control-Allow-Origin" not in _read(path, origin="https://evil.example")


# --- refusals --------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["old two-part form", "not a secret", "guessed secret", "date edited"])
def test_a_wrong_address_is_one_404(shared, kind):
    path = shared[1]["path"]
    prefix, secret = path.rsplit("/", 1)
    initials, _date = prefix.split("/")
    guess = {
        "old two-part form": prefix,
        "not a secret": f"{prefix}/short",
        "guessed secret": f"{prefix}/{'B' * 32}",
        "date edited": f"{initials}/01_Jan_2020/{secret}",
    }[kind]

    resp = _read(guess)
    assert (resp.status_code, resp.json()) == (404, GONE)


def test_a_token_that_fails_its_signature_is_404_and_removed(shop, shared):
    ShareLink.objects.filter(entity_id=shop.entity.id).update(token="tampered")

    resp = _read(shared[1]["path"])
    assert (resp.status_code, resp.json()) == (404, GONE)
    assert not ShareLink.objects.filter(entity_id=shop.entity.id).exists()


def test_a_token_for_another_day_is_404_and_kept(shop, shared):
    from datetime import date

    ShareLink.objects.filter(entity_id=shop.entity.id).update(
        token=share_token(shop.entity.id, date(2020, 1, 1), settings.SECRET_KEY))

    resp = _read(shared[1]["path"])
    assert (resp.status_code, resp.json()) == (404, GONE)
    assert ShareLink.objects.filter(entity_id=shop.entity.id).exists()


@pytest.mark.parametrize("change", ["draft", "deleted"])
def test_a_day_back_to_draft_or_gone_is_404(shared, change):
    report, body = shared
    if change == "draft":
        Report.objects.filter(id=report.id).update(status="draft")
    else:
        ReportExpense.objects.filter(report_id=report.id).delete()
        Report.objects.filter(id=report.id).delete()

    resp = _read(body["path"])
    assert (resp.status_code, resp.json()) == (404, GONE)


def test_a_link_whose_company_is_gone_is_404(shop, shared, monkeypatch):
    from petty_cash.services import share

    class _Nothing:
        @staticmethod
        def filter(**_kw):
            return _Nothing

        @staticmethod
        def first():
            return None

    monkeypatch.setattr(share.Entity, "objects", _Nothing)
    resp = _read(shared[1]["path"])
    assert (resp.status_code, resp.json()) == (404, GONE)


def test_an_expired_link_is_410_and_removed(shop, shared):
    ShareLink.objects.filter(entity_id=shop.entity.id).update(expires_at=datetime.now(UTC) - timedelta(minutes=1))

    resp = _read(shared[1]["path"])
    assert (resp.status_code, resp.json()) == (410, EXPIRED)
    assert not ShareLink.objects.filter(entity_id=shop.entity.id).exists()
    assert _read(shared[1]["path"]).status_code == 404


def test_a_failure_is_a_generic_500_and_the_secret_never_reaches_the_log(shared, monkeypatch, caplog):
    from petty_cash.routes import public_share

    path = shared[1]["path"]
    secret = path.rsplit("/", 1)[1]

    def boom(*_a, **_k):
        raise RuntimeError(f"database fell over near {secret}")

    monkeypatch.setattr(public_share, "shared_payload", boom)
    with caplog.at_level(logging.DEBUG):
        resp = _read(path)

    assert (resp.status_code, resp.json()) == (500, {"error": "Something went wrong on our side. Please try again."})
    assert "public share failure: RuntimeError" in caplog.text
    assert secret not in caplog.text


def test_reading_and_refusing_log_the_row_never_the_secret(shop, shared, caplog):
    path = shared[1]["path"]
    secret = path.rsplit("/", 1)[1]
    with caplog.at_level(logging.DEBUG):
        assert _read(path).status_code == 200
        assert _read(path.rsplit("/", 1)[0] + "/" + "C" * 32).status_code == 404
        assert _read("nothing").status_code == 404

    link = ShareLink.objects.get(entity_id=shop.entity.id)
    assert f"link={link.id}" in caplog.text
    assert secret not in caplog.text and "C" * 32 not in caplog.text
    endpoints = [getattr(r, "endpoint", "") for r in caplog.records if getattr(r, "endpoint", "")]
    assert endpoints and all(e == PUBLIC + "[redacted]" for e in endpoints)


# --- only this operation is public ------------------------------------------------------------------


def test_the_only_operation_without_the_bearer_door_is_this_get():
    """Every operation inherits the API's bearer door (``auth`` NOT_SET on the operation and its
    router) except this one, which says ``auth=None``."""
    from ninja.constants import NOT_SET

    from config.urls import api
    from core.auth import PettyCashBearerAuth

    assert [type(a) for a in api.auth] == [PettyCashBearerAuth]
    public = []
    for prefix, router in api._routers:
        assert router.auth is NOT_SET, prefix
        for path, view in router.path_operations.items():
            for operation in view.operations:
                if operation.auth_param is not NOT_SET:
                    public.append((prefix + path, sorted(operation.methods), operation.auth_param))
    assert public == [("/public/share/{path:share_path}", ["GET"], None)]


def test_a_post_to_it_is_not_served(shared):
    assert Client().post(PUBLIC + shared[1]["path"]).status_code == 405
