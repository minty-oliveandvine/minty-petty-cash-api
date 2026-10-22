"""The one error envelope every Minty API answers with (minty_shared.django.errors, user
2026-10-22): ``{"error": <sentence>, "fields"?: {...}, ...routing keys}``; 401 ``Unauthorized``;
422 for input the schema refuses; 500 the house sentence, its detail never in the response."""

from __future__ import annotations

import json

import pytest
from django.test import RequestFactory

pytestmark = pytest.mark.django_db

HOUSE = "Something went wrong on my end. Mind trying again?"


def _handled(exc):
    from config.urls import api

    request = RequestFactory().get("/api/petty-cash/v1/companies/x/home")
    response = api._lookup_exception_handler(exc)(request, exc)
    return response.status_code, json.loads(response.content)


def test_no_token_is_401_unauthorized(client):
    resp = client.get("/api/petty-cash/v1/companies/00000000-0000-0000-0000-000000000001/home")
    assert (resp.status_code, resp.json()) == (401, {"error": "Unauthorized"})


def test_an_unhandled_failure_is_the_house_sentence():
    assert _handled(RuntimeError("database fell over: secret-detail")) == (500, {"error": HOUSE})


def test_a_refusal_keeps_its_status_and_its_routing_keys():
    from core.exceptions import Refused

    status, body = _handled(Refused("Open the day first.", 409, to="opening", report_id="r1"))
    assert status == 409
    assert body == {"error": "Open the day first.", "to": "opening", "report_id": "r1"}


def test_a_refusal_about_one_input_names_it_in_fields_too():
    from core.exceptions import Refused

    status, body = _handled(Refused("Enter HKD 1.00 or less.", 422, field="bank_deposit"))
    assert status == 422
    assert body["field"] == "bank_deposit"
    assert body["fields"] == {"bank_deposit": "Enter HKD 1.00 or less."}


def test_schema_validation_is_422_naming_each_field():
    from ninja.errors import ValidationError

    exc = ValidationError([{"type": "missing", "loc": ("body", "payload", "amount"), "msg": "x"}])
    status, body = _handled(exc)
    assert status == 422
    assert body == {
        "error": "I still need amount to continue.",
        "fields": {"amount": "The amount is still empty. Add the amount to continue."},
    }
