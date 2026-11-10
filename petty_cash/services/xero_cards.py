"""Petty Cash Settings' Xero cards: whether Xero is live, the account mapping, the account codes.

Ported from Flask in Phase 3 of the Flask retirement (2026-11-06). It replaces the call to Flask's
``POST /api/internal/xero/petty-cash-settings``. The Flask sources:
- ``petty_cash_api/routes/settings.py``: ``xero_settings_answer``;
- ``entity/services/xero_mapping.py``;
- ``entity/services/xero_mapping_form_context.py``;
- ``entity/services/xero_account_mapping_post.py``: ``save_mapping``;
- ``entity/services/settings.py``: ``saveable_account_codes``, ``sync_entity_account_xero_active``.

On a live company, every answer first refreshes the company's accounts and contacts from Xero,
through accounts-api's one sync (``POST /api/internal/xero/sync``; the owner's choice, 2026-11-06).
A sync that fails is logged and the cards still open from what is stored. The lists are the
database's; a saved choice is always among its field's options.

Deliberate differences from Flask:
- A choice is resolved from the stored rows only. Flask's fallback (fetch it from Xero, or make an
  "Unknown Account" row) could not be reached: a choice the lists don't offer is refused first.
- When accounts-api can't be asked whether Xero is live, the read is a 503, not "not connected".
"""

from __future__ import annotations

import html
import logging

import requests
from django.conf import settings
from django.db import transaction
from minty_db.models import (
    AccountInfo,
    Entity,
    EntityAccountXero,
    EntityPettycashSettings,
    XeroContactSync,
)

from core.accounts_api import assertion_headers, service_assertion
from core.xero_tokens import XeroTokenUnavailable, xero_token
from petty_cash.services.publish import sync_connection_status
from petty_cash.services.xero_settings import mask_account_number, minty_on_its_own

logger = logging.getLogger("petty-cash-api")

SYNC_SCOPE = "xero-sync"
#: Two Xero fetches (accounts, then every page of contacts) behind accounts-api.
SYNC_TIMEOUT = (5, 60)
NOT_LIVE = "Xero isn't connected for this company. Connect it in the company's settings first."
UNREACHABLE = "I couldn't reach Xero's settings just now. Try again in a moment."
MAPPING_KEYS = ("main_bank", "deposit_bank", "cashsale_account", "cashsale_contact", "owners_account",
                "owners_contact", "discrepancy_account", "discrepancy_contact")
#: Petty Cash's code list (Flask's ``COA_INCLUDED_TYPES``).
COA_INCLUDED_TYPES = ("DIRECTCOSTS", "EXPENSE", "OVERHEADS", "PREPAYMENT")

#: (form key, label, kind, the list it chooses from, its entity_pettycash_settings column)
FIELDS = (
    ("main_bank", "Petty Cash Account", "account", "bank_accounts", "pettycash_account_id"),
    ("deposit_bank", "Deposit Bank Account", "account", "bank_accounts", "bank_account_id"),
    ("cashsale_account", "Cash Sales account code", "account", "cashsale_account", "cash_sale_account_id"),
    ("cashsale_contact", "Cash Sales contact", "contact", "contacts", "cash_sale_contact_id"),
    ("owners_account", "Director Personal Account code", "account", "owners_account", "director_account_id"),
    ("owners_contact", "Director / Responsible person", "contact", "contacts", "director_contact_id"),
    ("discrepancy_bank", "Discrepancy Bank Account", "account", "bank_accounts", "discrepancy_bank_account_id"),
    ("discrepancy_account", "Discrepancy account code", "account", "discrepancy_account", "discrepancy_account_id"),
    ("discrepancy_contact", "Discrepancy contact", "contact", "contacts", "discrepancy_contact_id"),
)
_LISTS = {
    "bank_accounts": lambda t: t == "BANK",
    "cashsale_account": lambda t: t in ("SALES", "REVENUE", "INCOME"),
    "owners_account": lambda t: t in ("NONCURRENT", "CURRLIAB", "TERMLIAB", "FIXED", "INVENTORY", "DIRECTCOSTS",
                                      "EXPENSE"),
    "discrepancy_account": lambda t: t in ("EXPENSE", "DIRECTCOSTS"),
}


class XeroUnreachable(Exception):
    """accounts-api could not say whether Xero is live."""


# ---- the connection --------------------------------------------------------------------------


def is_live(entity: Entity) -> bool:
    """A company with a Xero organisation whose token works and that Xero still lists as connected
    (``sync_connection_status`` reconciles ``status``; a connection revoked on Xero's site can leave
    the token working). Raises ``XeroUnreachable`` when accounts-api can't be asked."""
    if not entity.xero_org_id:
        return False
    try:
        token = xero_token(entity.id)
    except XeroTokenUnavailable as unavailable:
        if unavailable.reconnect:
            return False
        raise XeroUnreachable from None
    sync_connection_status(entity, token.access_token)
    entity.refresh_from_db(fields=["status"])
    return entity.status != "disconnected"


def refresh_from_xero(entity: Entity) -> None:
    """The company's accounts and contacts from Xero, through accounts-api. Logged, never raised:
    the cards still open from what is stored."""
    try:
        response = requests.post(f"{settings.ACCOUNTS_API_URL}/api/internal/xero/sync",
                                 headers=assertion_headers(service_assertion(SYNC_SCOPE, entity.id)),
                                 timeout=SYNC_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("Xero settings: the sync could not be asked for company %s: %s", entity.id, exc)
        return
    if response.status_code != 200:
        logger.warning("Xero settings: the sync answered %s for company %s", response.status_code, entity.id)


# ---- the lists -------------------------------------------------------------------------------


def _account(row: AccountInfo) -> dict:
    """Flask's ``account_info_to_xero_format``."""
    found = {"AccountID": row.xero_account_id, "Name": row.name, "Type": row.type, "Code": row.xero_code or "",
             "Status": row.status or "ACTIVE"}
    if row.type == "BANK" and row.bank_account_number:
        found["MaskedBankAccountNumber"] = mask_account_number(row.bank_account_number)
    return found


def _contact(row: XeroContactSync) -> dict:
    return {"ContactID": row.xero_contact_id, "Name": row.name}


def choices(entity_id) -> dict:
    """Each list's options (the ACTIVE accounts and the synced contacts) and each field's saved
    choice, which is always among its list's options (Flask's ``build_xero_mapping_form_context``)."""
    accounts = [_account(a) for a in AccountInfo.objects.filter(entity_id=entity_id, status="ACTIVE")]
    lists = {name: [a for a in accounts if keep(a["Type"])] for name, keep in _LISTS.items()}
    lists["contacts"] = [_contact(c) for c in XeroContactSync.objects.filter(entity_id=entity_id)]
    row = EntityPettycashSettings.objects.filter(entity_id=entity_id).first()
    saved = {}
    for key, _label, kind, listed, column in FIELDS:
        value = getattr(row, column, None) if row is not None else None
        model, convert = (XeroContactSync, _contact) if kind == "contact" else (AccountInfo, _account)
        found = model.objects.filter(id=value).first() if value else None
        saved[key] = convert(found) if found else None
        id_key = "ContactID" if kind == "contact" else "AccountID"
        if saved[key] and saved[key].get(id_key) and not any(o.get(id_key) == saved[key][id_key]
                                                             for o in lists[listed]):
            lists[listed] = [*lists[listed], saved[key]]
    return {"lists": lists, "saved": saved}


def read(entity_id) -> list[dict]:
    """One entry per field the person chooses (the Discrepancy Bank Account mirrors the Petty Cash
    Account, so it isn't one), with its options and its saved choice."""
    found = choices(entity_id)
    fields = []
    for key, label, kind, listed, _column in FIELDS:
        if key == "discrepancy_bank":
            continue
        id_key = "ContactID" if kind == "contact" else "AccountID"
        fields.append({
            "key": key, "label": label, "kind": kind,
            "options": [{"id": o.get(id_key), "name": o.get("Name") or "",
                         **({} if kind == "contact" else {"code": o.get("Code") or ""})}
                        for o in found["lists"][listed] if o.get(id_key)],
            "selected": (found["saved"][key] or {}).get(id_key),
        })
    return fields


def unoffered(entity_id, values: dict) -> list[str]:
    """The labels of fields whose value is not among their options (an account by id or code)."""
    lists = choices(entity_id)["lists"]
    labels = []
    for key, label, kind, listed, _column in FIELDS:
        value = values.get(key)
        if not value:
            continue
        if kind == "contact":
            offered = any(o.get("ContactID") == value for o in lists[listed])
        else:
            offered = any(value in (o.get("AccountID"), o.get("Code")) for o in lists[listed])
        if not offered:
            labels.append(label)
    return labels


def account_codes(entity_id) -> list[dict]:
    """Petty Cash's account codes in code order, ``{code, name, selected}`` (``selected`` =
    ``entity_account_xero.is_active``: the codes an expense line may use)."""
    rows = (EntityAccountXero.objects.select_related("account")
            .filter(account__entity_id=entity_id, account__type__in=COA_INCLUDED_TYPES)
            .order_by("account__xero_code"))
    return [{"code": r.account.xero_code or "", "name": html.unescape(r.account.name or ""), "selected": bool(r.is_active)}
            for r in rows]


# ---- the saves -------------------------------------------------------------------------------


def save_account_codes(entity: Entity, codes) -> str | None:
    """Tick exactly ``codes``; the refusal, or None. At least one of the company's listed codes
    stays ticked, unless it has none at all."""
    listed = EntityAccountXero.objects.filter(account__entity_id=entity.id).values("account_id")
    saveable = {str(c).strip() for c in AccountInfo.objects.filter(
        entity_id=entity.id, type__in=COA_INCLUDED_TYPES, id__in=listed,
    ).values_list("xero_code", flat=True) if c and str(c).strip()}
    wanted = {str(c).strip() for c in codes or [] if c and str(c).strip()}
    if saveable and not wanted & saveable:
        return "Pick at least one account code."
    with transaction.atomic():
        eligible = list(AccountInfo.objects.filter(entity_id=entity.id, type__in=COA_INCLUDED_TYPES))
        selected = {a.id for a in eligible if a.xero_code and a.xero_code.strip() in wanted}
        existing = {e.account_id: e for e in EntityAccountXero.objects.filter(account__entity_id=entity.id)}
        by_id = {a.id: a for a in eligible}
        for row in existing.values():
            row.is_active = row.account_id in selected
            account = by_id.get(row.account_id)
            if account is not None:
                row.name, row.type, row.xero_account_id = account.name, account.type, account.xero_account_id
                row.xero_org_id = entity.xero_org_id
            row.save()
        for account in eligible:
            if account.id not in existing:
                EntityAccountXero.objects.create(account_id=account.id, name=account.name, type=account.type,
                                                 xero_org_id=entity.xero_org_id, xero_account_id=account.xero_account_id,
                                                 is_active=account.id in selected)
    logger.info("Xero settings: %s of %s account codes ticked for company %s", len(selected), len(eligible), entity.id)
    return None


def save_mapping(entity: Entity, values: dict) -> list[str]:
    """Write the nine choices onto ``entity_pettycash_settings`` (in the caller's transaction). The
    labels of choices that match no stored row, if any - nothing is written then."""
    resolved, unresolved = {}, []
    for key, label, kind, _listed, column in FIELDS:
        value = values.get(key)
        if kind == "contact":
            row = XeroContactSync.objects.filter(entity_id=entity.id, xero_contact_id=value).first()
        else:
            row = (AccountInfo.objects.filter(entity_id=entity.id, xero_account_id=value).first()
                   or AccountInfo.objects.filter(entity_id=entity.id, xero_code=value).first())
        if row is None:
            unresolved.append(f"{label} ({value})" if value else label)
        else:
            resolved[column] = row.id
    if unresolved:
        return unresolved
    EntityPettycashSettings.objects.update_or_create(entity_id=entity.id, defaults=resolved)
    return []


# ---- a company keeping its books in Minty (Xero optional, 2026-11-10) ----------------------------

#: The five accounts daily closing uses - the mapping's account fields, minus the discrepancy bank,
#: which follows the petty cash account. No contact: contacts are Xero's.
MINTY_KEYS = ("main_bank", "deposit_bank", "cashsale_account", "owners_account", "discrepancy_account")


def _minty_options(entity_id) -> dict[str, list[dict]]:
    """Each list's options among the company's own active accounts, named by ``public_id``."""
    from petty_cash.services.expenses import public_id  # the expense screens' naming, one place

    accounts = list(AccountInfo.objects.filter(entity_id=entity_id, status="ACTIVE", record_status="active")
                    .order_by("xero_code", "name"))
    return {name: [{"id": public_id(a), "name": a.name, "code": a.xero_code or "", "row": a.id}
                   for a in accounts if keep(a.type)] for name, keep in _LISTS.items()}


def minty_read(entity_id) -> list[dict]:
    """The mapping card's fields for such a company: the five accounts, chosen from its own."""
    options = _minty_options(entity_id)
    row = EntityPettycashSettings.objects.filter(entity_id=entity_id).first()
    fields = []
    for key, label, _kind, listed, column in FIELDS:
        if key not in MINTY_KEYS:
            continue
        chosen = getattr(row, column, None) if row is not None else None
        offered = options[listed]
        fields.append({
            "key": key, "label": label, "kind": "account",
            "options": [{"id": o["id"], "name": o["name"], "code": o["code"]} for o in offered],
            "selected": next((o["id"] for o in offered if o["row"] == chosen), None),
        })
    return fields


def minty_save(entity: Entity, body: dict, may_edit: bool) -> tuple[dict, int]:
    """Write the five choices (and the discrepancy bank, which follows the petty cash account)."""
    values = {key: str(body.get(key) or "").strip() for key in MINTY_KEYS}
    labels = {key: label for key, label, *_ in FIELDS}
    missing = [labels[key] for key in MINTY_KEYS if not values[key]]
    if missing:
        return {"error": " ".join(f"Choose the {label}." for label in missing)}, 422
    if values["main_bank"] == values["deposit_bank"]:
        return {"error": "The Petty Cash Account and the Deposit Bank Account can't be the same. "
                         "Choose a different one for each."}, 422
    options = _minty_options(entity.id)
    resolved, not_offered = {}, []
    for key, label, _kind, listed, column in FIELDS:
        if key not in MINTY_KEYS:
            continue
        found = next((o["row"] for o in options[listed] if o["id"] == values[key]), None)
        if found is None:
            not_offered.append(label)
        resolved[column] = found
    if not_offered:
        return {"error": " ".join(f"Choose the {label} from the list." for label in not_offered)}, 422
    resolved["discrepancy_bank_account_id"] = resolved["pettycash_account_id"]
    first_save = not EntityPettycashSettings.objects.filter(entity_id=entity.id).exists()
    EntityPettycashSettings.objects.update_or_create(entity_id=entity.id, defaults=resolved)
    logger.info("Account mapping saved for company %s (Minty on its own)", entity.id)
    return {**payload(entity, may_edit), "message": "Account mapping saved.", "first_save": first_save}, 200


# ---- the answer ------------------------------------------------------------------------------


def payload(entity: Entity, may_edit: bool) -> dict:
    if minty_on_its_own(entity.id):
        return {
            "live": False,
            "minty": True,
            "organisation": None,
            "mapping": minty_read(entity.id),
            "mapping_saved": EntityPettycashSettings.objects.filter(entity_id=entity.id).exists(),
            "account_codes": account_codes(entity.id),
            "permissions": {"edit_mapping": may_edit, "edit_account_codes": may_edit},
        }
    live = is_live(entity)
    if live:
        refresh_from_xero(entity)
    return {
        "live": live,
        "organisation": entity.xero_tenant_name if live else None,
        "mapping": read(entity.id) if live else None,
        "mapping_saved": EntityPettycashSettings.objects.filter(entity_id=entity.id).exists(),
        "account_codes": account_codes(entity.id) if live else None,
        "permissions": {"edit_mapping": may_edit, "edit_account_codes": may_edit},
    }


def answer(entity_id, action: str, body, *, may_edit: bool) -> tuple[dict, int]:
    """``read``, ``mapping`` or ``account-codes`` (the route has checked the person's rights)."""
    entity = Entity.objects.get(id=entity_id)
    if minty_on_its_own(entity_id) and action != "read":
        body = body if isinstance(body, dict) else {}
        if action == "mapping":
            return minty_save(entity, body, may_edit)
        codes = body.get("codes")
        if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
            return {"error": "Send the ticked account codes as a list."}, 422
        problem = save_account_codes(entity, codes)
        if problem:
            return {"error": problem}, 422
        return {**payload(entity, may_edit), "message": "Account codes saved."}, 200
    try:
        if action == "read":
            return payload(entity, may_edit), 200
        if not is_live(entity):
            return {"error": NOT_LIVE}, 409
    except XeroUnreachable:
        return {"error": UNREACHABLE}, 503
    body = body if isinstance(body, dict) else {}

    if action == "account-codes":
        codes = body.get("codes")
        if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
            return {"error": "Send the ticked account codes as a list."}, 422
        problem = save_account_codes(entity, codes)
        if problem:
            return {"error": problem}, 422
        return {**payload(entity, may_edit), "message": "Account codes saved."}, 200

    values = {key: str(body.get(key) or "").strip() for key in MAPPING_KEYS}
    labels = {key: label for key, label, *_ in FIELDS}
    missing = [labels[key] for key in MAPPING_KEYS if not values[key]]
    if missing:
        return {"error": " ".join(f"Choose the {label}." for label in missing)}, 422
    if values["main_bank"] == values["deposit_bank"]:
        return {"error": "The Petty Cash Account and the Deposit Bank Account can't be the same. "
                         "Choose a different one for each."}, 422
    values["discrepancy_bank"] = values["main_bank"]  # it mirrors the Petty Cash Account
    not_offered = unoffered(entity.id, values)
    if not_offered:
        return {"error": " ".join(f"Choose the {label} from the list." for label in not_offered)}, 422
    first_save = not EntityPettycashSettings.objects.filter(entity_id=entity.id).exists()
    with transaction.atomic():
        unresolved = save_mapping(entity, values)
    if unresolved:
        return {"error": "Some choices haven't synced from Xero yet: " + "; ".join(unresolved)
                         + ". Try again once the Xero sync has finished."}, 409
    return {**payload(entity, may_edit), "message": "Xero account mapping saved.", "first_save": first_save}, 200
