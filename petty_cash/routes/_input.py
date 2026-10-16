"""Reading a request the way Flask's Petty Cash API does: the JSON body by hand (so a refusal is
Flask's own sentence, not a schema dump), the edit permission, the day in the query."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from core.exceptions import Refused
from core.policy import Permission, has_permission
from petty_cash.days import parse_day
from petty_cash.money import MAX_AMOUNT, cents

NOT_ALLOWED = "You do not have permission to edit reports for this entity."
BAD_DATE = "That date doesn't look quite right! Please check the format and try again."


def require_edit(request) -> None:
    """``REPORT_EDIT_OWN`` on the company, as every step's page asks."""
    if not has_permission(request.auth_user, Permission.REPORT_EDIT_OWN, request.company.id):
        raise Refused(NOT_ALLOWED, 403)


def json_body(request):
    """The JSON body, or None when there is none or it does not parse (Flask's
    ``get_json(silent=True)``)."""
    try:
        return json.loads(request.body or b"null")
    except (ValueError, UnicodeDecodeError):
        return None


def query_day(request, *, required: bool):
    """``?transaction_date=``: a date, None when absent and optional, else 422."""
    raw = request.GET.get("transaction_date")
    if not raw:
        if required:
            raise Refused(BAD_DATE, 422)
        return None
    day = parse_day(raw)
    if day is None:
        raise Refused(BAD_DATE, 422)
    return day


def amount(value, *, allow_zero=True) -> Decimal | None:
    """A JSON number within Flask's bounds as cents, or None. Booleans and strings are refused,
    as Flask's ``isinstance(amount, (int, float))`` refuses them."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0 or number > MAX_AMOUNT or (not allow_zero and number == 0):
        return None
    return cents(number)


def action_type(body: dict) -> str | None:
    """``action: "next" | "exit"`` as the services' ``save_next`` / ``save_exit``."""
    return {"next": "save_next", "exit": "save_exit"}.get(body.get("action"))


def form_and_files(request):
    """The form fields and files of a PUT or PATCH, as Flask's ``request.form`` / ``request.files``
    read them for any method (Django parses a form body only for POST)."""
    if request.method == "POST":
        return request.POST, request.FILES
    method = request.method
    request.method = "POST"
    try:
        request._load_post_and_files()
    finally:
        request.method = method
    return request._post, request._files
