"""Petty Cash settings - Flask's ``petty_cash_api/routes/settings.py``: Country & currency and the
Electronic / Delivery sales methods here; the Xero cards forwarded to Flask.

- ``GET /settings``: the country and currency with the lists offered, the two method lists, the
  catalogue methods not on them, whether Xero is connected, and what this person may change.
- ``PUT /settings/country`` ``{country_code, currency_id}``.
- ``POST /sales-methods`` ``{type, sale_info_id | name}``, ``PUT /sales-methods/{id}`` ``{name}``,
  ``DELETE /sales-methods/{id}``, ``PUT /sales-methods/order`` ``{type, method_ids}``.
- ``GET /settings/xero``, ``PUT /settings/xero/mapping``, ``PUT /settings/xero/account-codes``: the
  Xero cards, which check the connection live and refresh from Xero, so Flask does them
  (``core/accounts_api.py::forward_xero_settings``, the owner's choice: this service holds no Xero
  code) and its answer comes back unchanged. The rights are checked here first (``COA_VIEW`` to
  read, ``COUNTRY_RIGHTS`` to save; 403, no call); the connection, the 409s and 422s are Flask's.

Every write answers the whole settings payload with a ``message``. Name lengths are the schema's -
``sale_info.sale_name`` and ``value_name`` are 80 characters, and a new method's ``value_name`` is
its name + ``_sales`` - where Flask's 100-character check let a 75-100 character name through to
a database error.
"""

from __future__ import annotations

import uuid

from django.http import JsonResponse
from ninja import Router

from core.accounts_api import forward_xero_settings
from core.exceptions import Refused
from core.policy import Permission, has_permission
from petty_cash.routes._input import json_body
from petty_cash.services.settings import (
    TYPES,
    SettingsRefused,
    add_method,
    available,
    checked_country_currency,
    country_currency_choices,
    methods,
    remove_method,
    rename_method,
    reorder_methods,
    save_country_currency,
)
from shared_models.models import Entity, SaleInfo

router = Router()

NO_VIEW = "You don't have permission to see these settings."

#: the most a NEW method's name may hold: its value_name (name + "_sales") must fit 80 characters
ADD_NAME_MAX = 80 - len("_sales")
#: a rename changes sale_name only
RENAME_NAME_MAX = 80
#: the Settings page's Save, which writes the country only with all four
COUNTRY_RIGHTS = ("ENTITY_UPDATE", "COA_UPDATE", "COA_CREATE", "COA_DELETE")
NO_RIGHT = "You don't have permission to change these settings. Ask an Accountant or an Admin."
NOT_FOUND = "That sales method isn't on this company's list."


def _may(request, *names) -> bool:
    return all(has_permission(request.auth_user, getattr(Permission, n), request.company.id) for n in names)


def _body(request) -> dict:
    data = json_body(request)
    return data if isinstance(data, dict) else {}


def _uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def _name(raw, limit: int, verb: str) -> str:
    name = str(raw or "").strip()
    if not name:
        raise Refused(f"The method's name is still empty. Type a name to {verb} it.", 422)
    if len(name) > limit:
        raise Refused(f"That name is too long. Keep it to {limit} characters or fewer.", 422)
    return name


def settings_payload(request) -> dict:
    entity_id = request.company.id
    org = Entity.objects.get(id=entity_id)
    may_view_sales = _may(request, "SALES_METHOD_VIEW")
    return {
        "country": country_currency_choices(org),
        "sales_methods": methods(entity_id) if may_view_sales else {t: [] for t in TYPES},
        "available": available(entity_id) if may_view_sales else {t: [] for t in TYPES},
        "xero_connected": bool(org.xero_org_id),
        "permissions": {
            "edit_country": _may(request, *COUNTRY_RIGHTS),
            "view_sales_methods": may_view_sales,
            "add_sales_method": _may(request, "SALES_METHOD_CREATE"),
            "rename_sales_method": _may(request, "SALES_METHOD_UPDATE"),
            "remove_sales_method": _may(request, "SALES_METHOD_DELETE"),
            "reorder_sales_methods": _may(request, "SALES_METHOD_REORDER"),
        },
    }


def _answer(request, message: str, status: int = 200):
    return JsonResponse({**settings_payload(request), "message": message}, status=status)


def _listed(entity_id, method_id) -> bool:
    """One of the company's Electronic / Delivery methods - Cash is managed on the report."""
    return any(m["id"] == str(method_id) for group in methods(entity_id).values() for m in group)


@router.get("/settings")
def settings_page(request):
    if not _may(request, "COA_VIEW"):
        raise Refused(NO_VIEW, 403)
    return settings_payload(request)


@router.get("/settings/xero")
def xero_settings(request):
    if not _may(request, "COA_VIEW"):
        raise Refused(NO_VIEW, 403)
    return _xero(request, "read", None)


@router.put("/settings/xero/mapping")
def save_xero_mapping(request):
    """``{main_bank, deposit_bank, cashsale_account, cashsale_contact, owners_account,
    owners_contact, discrepancy_account, discrepancy_contact}`` - Xero ids from the read's options."""
    if not _may(request, *COUNTRY_RIGHTS):
        raise Refused(NO_RIGHT, 403)
    return _xero(request, "mapping", _body(request))


@router.put("/settings/xero/account-codes")
def save_xero_account_codes(request):
    """``{codes: [..]}``: exactly these account codes ticked for Petty Cash (at least one)."""
    if not _may(request, *COUNTRY_RIGHTS):
        raise Refused(NO_RIGHT, 403)
    return _xero(request, "account-codes", _body(request))


def _xero(request, action: str, body):
    answer, status = forward_xero_settings(request.company.id, request.auth_user.id, action, body)
    return JsonResponse(answer, status=status)


@router.put("/settings/country")
def save_country(request):
    if not _may(request, *COUNTRY_RIGHTS):
        raise Refused(NO_RIGHT, 403)
    data = _body(request)
    if not str(data.get("country_code") or "").strip() and not str(data.get("currency_id") or "").strip():
        raise Refused("Choose a country or a currency to save.", 422)
    try:
        save_country_currency(request.company.id, checked_country_currency(data))
    except SettingsRefused as refused:
        raise Refused(refused.message, refused.status) from None
    return _answer(request, "Country and currency saved.")


@router.post("/sales-methods")
def add_sales_method(request):
    if not _may(request, "SALES_METHOD_CREATE"):
        raise Refused(NO_RIGHT, 403)
    data = _body(request)
    kind = data.get("type")
    if kind not in TYPES:
        raise Refused("Choose Electronic or Delivery for the method.", 422)
    entity_id = request.company.id
    sale_info_id = None
    if data.get("sale_info_id"):
        sale = SaleInfo.objects.filter(id=str(data["sale_info_id"])).first() if _uuid(data["sale_info_id"]) else None
        if sale is None or not sale.enabled or str(sale.type) != kind:
            raise Refused("Pick one of the methods offered, or type a new name.", 422)
        sale_info_id, name = str(sale.id), sale.sale_name
    else:
        name = _name(data.get("name"), ADD_NAME_MAX, "add")
    if any(m["name"].casefold() == name.casefold() for m in methods(entity_id)[kind]):
        raise Refused(f"{name} is already on the list.", 409)
    add_method(entity_id, kind, sale_info_id=sale_info_id, name=name)
    return _answer(request, f"{name} added.", 201)


@router.put("/sales-methods/order")
def reorder_sales_methods(request):
    if not _may(request, "SALES_METHOD_REORDER"):
        raise Refused(NO_RIGHT, 403)
    data = _body(request)
    kind, ids = data.get("type"), data.get("method_ids")
    if kind not in TYPES or not isinstance(ids, list):
        raise Refused("Send the type and the methods in their new order.", 422)
    entity_id = request.company.id
    current = [m["id"] for m in methods(entity_id)[kind]]
    if sorted(str(i) for i in ids) != sorted(current):
        raise Refused("The list changed since it was loaded. Reload the page and order it again.", 409)
    # one type at a time, as the page's cards save it: the other type keeps its own order
    reorder_methods(entity_id, [str(i) for i in ids])
    return _answer(request, "Order saved.")


@router.put("/sales-methods/{method_id}")
def rename_sales_method(request, method_id: str):
    if not _may(request, "SALES_METHOD_UPDATE"):
        raise Refused(NO_RIGHT, 403)
    entity_id = request.company.id
    if not _uuid(method_id) or not _listed(entity_id, method_id):
        raise Refused(NOT_FOUND, 404)
    name = _name(_body(request).get("name"), RENAME_NAME_MAX, "save")
    try:
        rename_method(entity_id, str(uuid.UUID(method_id)), name)
    except SettingsRefused as refused:
        raise Refused(refused.message, refused.status) from None
    return _answer(request, "Name saved.")


@router.delete("/sales-methods/{method_id}")
def remove_sales_method(request, method_id: str):
    if not _may(request, "SALES_METHOD_DELETE"):
        raise Refused(NO_RIGHT, 403)
    entity_id = request.company.id
    if not _uuid(method_id) or not _listed(entity_id, method_id):
        raise Refused(NOT_FOUND, 404)
    remove_method(entity_id, str(uuid.UUID(method_id)))
    return _answer(request, "Method removed. Reports that used it keep their amounts.")
