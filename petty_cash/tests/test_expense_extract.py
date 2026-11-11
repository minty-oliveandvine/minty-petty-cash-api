"""Reading a receipt with the model: ``POST /reports/expenses/extract`` (``petty_cash/routes/
expenses.py``) and ``petty_cash/services/receipt_reader.py``, on ``minty_shared.ai`` (2026-11-04:
the reader moved here from Flask's ``services/expense_ai.py``, whose receipt cases these are; the
engine's own cases are minty-shared-py's ``tests/test_ai.py``).

No network: the model client is a stand-in - a test that reached Google would test Google."""

from __future__ import annotations

import json
import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from minty_db.enums import EntityRole
from minty_db.models import AccountInfo, UserEntity, XeroContactSync
from minty_shared import ai

from petty_cash.services import receipt_reader
from petty_cash.tests.conftest import WEB, headers, make_user, url
from petty_cash.tests.test_expenses import JPEG, PNG, lists  # noqa: F401 - the lists fixture

# Every test runs on the shop with its lists (test_expenses.lists): two suppliers, one account on.
pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("lists")]


def _reply(**over) -> str:
    """A well-formed reply naming the shop's lists (``lists``: con-0001 ABC Supplies, acc-0001
    400 Advertising switched on, acc-0002 410 Cleaning switched off), confident everywhere."""
    body = {
        "supplier_contact_id": "con-0001", "supplier_name": "ABC Supplies", "supplier_confidence": 0.93,
        "account_id": "acc-0001", "account_code": "400", "account_name": "Advertising",
        "account_confidence": 0.88, "amount_value": 120.0, "amount_confidence": 0.97,
        "description_value": "Flyers", "description_confidence": 0.81, "currency": "HKD",
        "document_date": "2026-08-28", "document_date_confidence": 0.9,
    }
    body.update(over)
    return json.dumps(body)


class FakeUsage:
    total_input_tokens = 5000
    total_output_tokens = 300
    total_thought_tokens = 400
    total_cached_tokens = 0


class FakeInteraction:
    def __init__(self, output_text=None, status="completed"):
        self.id = "interaction-1"
        self.output_text = output_text
        self.status = status
        self.errors = []
        self.usage = FakeUsage()


class FakeModel:
    """The google-genai client's ``interactions.create``."""

    def __init__(self):
        self.answer = FakeInteraction(_reply())
        self.requests = []
        self.interactions = self

    def create(self, **request):
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def model(monkeypatch) -> FakeModel:
    fake = FakeModel()
    monkeypatch.setattr(ai, "get_client", lambda timeout_s: fake)
    monkeypatch.setattr(ai.time, "sleep", lambda _s: None)
    for name, value in {"EXPENSE_AI_ENABLED": "true", "EXPENSE_AI_CONF_HIGH": "0.85",
                        "EXPENSE_AI_CONF_MEDIUM": "0.60", "EXPENSE_AI_MODEL": "gemini-3.8-flash"}.items():
        monkeypatch.setenv(name, value)
    receipt_reader.LIMITER.reset()
    return fake


def _extract(client, shop, user=None, *, receipt=PNG, name="slip.jpg", files=None):
    form = {}
    if files is not None:
        form["receipt"] = files
    elif receipt is not None:
        form["receipt"] = SimpleUploadedFile(name, receipt, content_type="image/jpeg")
    return client.post(url(shop.entity.id, "/reports/expenses/extract"), form,
                       headers=headers((user or shop.owner).id))


# --- the route ---------------------------------------------------------------------------------


def test_a_receipt_is_read_against_the_company_s_own_lists(client, shop, model):
    resp = _extract(client, shop)

    assert resp.status_code == 200, resp.content
    assert resp["Access-Control-Allow-Origin"] == WEB and resp["Cache-Control"] == "no-store"
    body = resp.json()
    assert body["reason"] is None
    s = body["suggestions"]
    assert s["supplier"] == {"value": "ABC Supplies", "confidence": 0.93, "band": "high",
                             "applied": True, "contact_id": "con-0001"}
    assert s["account"]["account_id"] == "acc-0001" and s["account"]["value"] == "400 Advertising"
    assert s["amount"]["value"] == "120.00" and s["description"]["value"] == "Flyers"
    assert s["document_date"]["value"] == "2026-08-28"
    assert (s["currency"], s["entity_currency"]) == ("HKD", "HKD")
    # the model read the bytes as sent, typed by the bytes (a PNG named .jpg sent as image/jpeg)
    [request] = model.requests
    assert request["input"][1]["type"] == "image"


def test_the_prefix_carries_only_what_a_line_can_use_in_a_stable_order(client, shop, model):
    _extract(client, shop)

    prefix = model.requests[0]["input"][0]["text"]
    assert prefix.startswith("## Entity\n")
    assert "Base currency: HKD" in prefix
    assert "acc-0001 | 400 | Advertising" in prefix
    assert "acc-0002" not in prefix  # switched off for Petty Cash: never suggested
    # suppliers A to Z, so the cached prefix is byte-stable between reads
    assert prefix.index("con-0001 | ABC Supplies") < prefix.index("con-0002 | Taxi Co")
    assert model.requests[0]["system_instruction"] == receipt_reader.INSTRUCTION
    assert "acc-0001" not in json.dumps(model.requests[0]["response_format"]["schema"])


@pytest.mark.parametrize("env, reason", [
    ({"EXPENSE_AI_ENABLED": "false"}, "disabled"),
    ({"EXPENSE_AI_RATE_USER_PER_MIN": "0"}, "rate_limited"),
    ({"EXPENSE_AI_MAX_FILE_MB": "0"}, "file_too_large"),
], ids=["off", "rate-limited", "too-large"])
def test_refusals_past_the_checks_are_a_silent_no_suggestion(client, shop, model, monkeypatch, env, reason):
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    resp = _extract(client, shop)

    assert resp.status_code == 200 and resp.json() == {"suggestions": None, "reason": reason}
    assert model.requests == []


def test_a_company_with_nothing_to_choose_from_asks_no_question(client, shop, model):
    XeroContactSync.objects.filter(entity_id=shop.entity.id).delete()
    AccountInfo.objects.filter(entity_id=shop.entity.id).delete()

    resp = _extract(client, shop)

    assert resp.status_code == 200 and resp.json() == {"suggestions": None, "reason": "no_context"}
    assert model.requests == []


def test_a_failure_of_the_model_is_a_silent_no_suggestion(client, shop, model):
    model.answer = TimeoutError("deadline exceeded")

    resp = _extract(client, shop)

    assert resp.status_code == 200 and resp.json() == {"suggestions": None, "reason": "timeout"}


def test_one_log_line_with_numbers_and_never_the_values(client, shop, model, caplog):
    caplog.set_level("INFO", logger="minty-shared.ai")

    _extract(client, shop)

    [line] = [r.getMessage() for r in caplog.records if "expense_ai: extract" in r.getMessage()]
    assert f"entity={shop.entity.id}" in line and "tokens_in=5000" in line and "status=ok" in line
    # the ids are random: one holding "120" once failed CI (2026-11-11), so they go before the check
    bare = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", line)
    assert "ABC Supplies" not in bare and "120" not in bare and "Flyers" not in bare


@pytest.mark.parametrize("receipt, message", [
    (None, "Attach the receipt to read it."),
    (b"GIF89a-not-allowed", "Attach a PDF, JPEG or PNG file."),
    (b"", "That file is empty. Attach the receipt again."),
    (b"%PDF" + b"0" * (10 * 1024 * 1024), "Attach a file of 10 MB or less."),
], ids=["missing", "gif", "empty", "too-big"])
def test_a_file_that_is_not_a_receipt_is_refused_here(client, shop, model, receipt, message):
    resp = _extract(client, shop, receipt=receipt)

    assert resp.status_code == 422 and resp.json() == {"error": message}
    assert model.requests == []


def test_two_files_are_refused_here(client, shop, model):
    files = [SimpleUploadedFile("a.jpg", JPEG), SimpleUploadedFile("b.jpg", JPEG)]

    resp = _extract(client, shop, files=files)

    assert resp.status_code == 422 and resp.json() == {"error": "Attach one receipt at a time."}
    assert model.requests == []


def test_who_may_ask_is_decided_here(client, shop, model):
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
    assert model.requests == []


# --- the reply is untrusted input --------------------------------------------------------------


def _read(client, shop, model, **reply):
    model.answer = FakeInteraction(_reply(**reply))
    body = _extract(client, shop).json()
    return body["suggestions"], body["reason"]


def test_a_supplier_id_not_sent_is_dropped_and_the_rest_survives(client, shop, model):
    s, _ = _read(client, shop, model, supplier_contact_id="con-invented")
    assert s["supplier"]["contact_id"] == "" and s["supplier"]["applied"] is False
    assert s["amount"]["applied"] is True


def test_an_account_id_not_sent_is_dropped(client, shop, model):
    s, _ = _read(client, shop, model, account_id="acc-0002")  # exists, but switched off
    assert s["account"]["account_id"] == "" and s["account"]["applied"] is False


def test_a_plausible_name_does_not_rescue_a_bad_id(client, shop, model):
    s, _ = _read(client, shop, model, supplier_contact_id="", supplier_name="ABC Supplies")
    assert s["supplier"]["contact_id"] == ""


def test_an_unmatched_supplier_name_is_offered_capped(client, shop, model):
    s, _ = _read(client, shop, model, supplier_contact_id="", supplier_name="Caspita Restaurant")
    assert (s["supplier"]["applied"], s["supplier"]["detected_name"]) == (False, "Caspita Restaurant")
    s, _ = _read(client, shop, model, supplier_contact_id="", supplier_name="z" * 400)
    assert len(s["supplier"]["detected_name"]) == 100
    s, _ = _read(client, shop, model)
    assert "detected_name" not in s["supplier"]


@pytest.mark.parametrize("amount", [0, -50, 99_999_999_999])
def test_an_out_of_range_amount_is_dropped(client, shop, model, amount):
    s, _ = _read(client, shop, model, amount_value=amount)
    assert s["amount"]["value"] == "" and s["amount"]["applied"] is False


def test_the_description_is_capped_and_an_unknown_currency_dropped(client, shop, model):
    s, _ = _read(client, shop, model, description_value="x" * 5000, currency="dollars")
    assert len(s["description"]["value"]) == 200 and s["currency"] == ""


def test_low_is_kept_for_the_log_but_not_prefilled_and_medium_is(client, shop, model):
    s, _ = _read(client, shop, model, amount_confidence=0.2, description_confidence=0.7)
    assert (s["amount"]["band"], s["amount"]["applied"], s["amount"]["value"]) == ("low", False, "120.00")
    assert (s["description"]["band"], s["description"]["applied"]) == ("medium", True)


def test_nothing_usable_says_so(client, shop, model):
    low = {k: 0.1 for k in ("supplier_confidence", "account_confidence", "amount_confidence",
                            "description_confidence", "document_date_confidence")}
    _, reason = _read(client, shop, model, **low)
    assert reason == "no_usable_field"


def test_the_instruction_asks_for_english_and_keeps_the_supplier_name():
    instruction = receipt_reader.INSTRUCTION
    assert "supplier_name IS AN EXCEPTION" in instruction
    assert "ALWAYS write description_value in English" in instruction
    assert "Write supplier_name in English only" in instruction


# --- the connectivity check (Flask's `flask expense-ai check`, moved here) ----------------------


def test_the_check_command_sends_a_generated_image_and_reports_the_round_trip(model, capsys):
    from django.core.management import call_command

    model.answer = FakeInteraction(_reply(supplier_contact_id="", account_id="", amount_value=0,
                                          description_value="", document_date=""))
    call_command("expense_ai_check")

    out = capsys.readouterr().out
    assert "model            : gemini-3.8-flash" in out and "Round trip OK" in out
    assert "tokens in / out  : 5000 / 300" in out
    [request] = model.requests
    assert request["input"][1]["mime_type"] == "image/png"
    assert "Connectivity check" in request["input"][0]["text"]


def test_the_check_command_fails_loudly_when_the_model_does_not_answer(model):
    from django.core.management import CommandError, call_command

    model.answer = TimeoutError("deadline exceeded")
    with pytest.raises(CommandError, match="FAILED: timeout"):
        call_command("expense_ai_check")
