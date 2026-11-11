"""The company's Petty Cash Xero roles - Flask's ``blueprints/xero/services/settings.py``
(``get_entity_account_settings``, ``check_entity_xero_settings_complete``,
``get_missing_xero_settings_fields``).

Flask reads the ``entity_setup_summary`` view; this reads the same columns of
``entity_pettycash_settings`` and joins ``account_info`` / ``xero_contact_sync`` the way the view
does, so the answer is the view's: a slot is set when its row id is set and the row exists.
"""

from __future__ import annotations

from minty_db.models import AccountInfo, Entity, EntityPettycashSettings, XeroContactSync

#: The nine slots, in the dashboard's order (Flask's ``blueprints/shared/views.PETTYCASH_SLOTS``):
#: (settings column, label, kind).
PETTYCASH_SLOTS = (
    ("pettycash_account_id", "Main Bank Account", "account"),
    ("bank_account_id", "Deposit Bank Account", "account"),
    ("cash_sale_account_id", "Cash Sale Account", "account"),
    ("cash_sale_contact_id", "Cash Sale Contact", "contact"),
    ("director_account_id", "Owners Account", "account"),
    ("director_contact_id", "Owners Contact", "contact"),
    ("discrepancy_account_id", "Discrepancy Account", "account"),
    ("discrepancy_bank_account_id", "Discrepancy Bank Account", "account"),
    ("discrepancy_contact_id", "Discrepancy Contact", "contact"),
)

#: A company keeping its books in Minty (Xero optional, 2026-11-10) needs only the five accounts
#: daily closing uses, named as onboarding's Accounting Setup (4.5) names them. No contact: those are
#: Xero's; the discrepancy bank follows the petty cash account, as it does for a Xero company.
MINTY_SLOTS = (
    ("pettycash_account_id", "Petty cash"),
    ("bank_account_id", "Bank you deposit into"),
    ("cash_sale_account_id", "Cash sales"),
    ("director_account_id", "Paid personally"),
    ("discrepancy_account_id", "Cash difference"),
)


#: The cash-sale choices, which a company banking all its sales doesn't need (owner's brief,
#: 2026-11-10: its Cash method is off, so it never records cash sales to post).
CASH_SALE_COLUMNS = ("cash_sale_account_id", "cash_sale_contact_id")


def banks_all_sales(entity_id) -> bool:
    """Onboarding's "We bank all sales" (``entities.uses_sales_cash = false``)."""
    return Entity.objects.filter(id=entity_id, uses_sales_cash=False).exists()


def minty_on_its_own(entity_id) -> bool:
    """The company keeps its books in Minty (``setup_mode = 'minty'``). Until its accounts are matched
    to Xero, it stays Minty's - an organisation recorded mid-matching (Settings Phase 2, 2026-11-11)
    changes nothing; matching's Finish sets ``setup_mode = 'xero'`` and Xero's rules apply."""
    return Entity.objects.filter(id=entity_id, setup_mode="minty").exists()


_ROLE_COLUMN = {
    "pettycash": "pettycash_account_id",
    "bank": "bank_account_id",
    "cash_sale": "cash_sale_account_id",
    "discrepancy": "discrepancy_bank_account_id",
    "discrepancy_account": "discrepancy_account_id",
    "director": "director_account_id",
}


def mask_account_number(account_number) -> str:
    """``****1234`` - Flask's ``services/helpers/xero.mask_account_number``."""
    if not account_number:
        return ""
    return f"****{str(account_number)[-4:]}"


def _settings(entity_id) -> EntityPettycashSettings | None:
    return EntityPettycashSettings.objects.filter(entity_id=entity_id).first()


def account_for_role(entity_id, role: str) -> dict | None:
    """The account mapped to a Petty Cash role, in Flask's shape, or None."""
    column = _ROLE_COLUMN.get(role)
    row = _settings(entity_id)
    account_id = getattr(row, column, None) if (row is not None and column) else None
    account = AccountInfo.objects.filter(id=account_id).first() if account_id else None
    if account is None:
        return None
    found = {
        "account_id": account.id,
        "xero_account_id": account.xero_account_id,
        "name": account.name,
        "account_code": account.xero_code or "",
    }
    if role not in ("director", "cash_sale", "discrepancy"):
        found["bank_account_number"] = mask_account_number(account.bank_account_number)
    return found


#: A contact role -> its ``entity_pettycash_settings`` column (Flask's
#: ``xero_bridge._CONTACT_ROLE_TO_PETTYCASH_SETTINGS_COLUMN``).
_CONTACT_ROLE_COLUMN = {
    "cashsale_contact": "cash_sale_contact_id",
    "director_contact": "director_contact_id",
    "discrepancy_contact": "discrepancy_contact_id",
}


def contact_for_role(entity_id, role: str) -> XeroContactSync | None:
    """The synced contact mapped to a Petty Cash contact role, or None (Flask's
    ``get_entity_contact_settings``)."""
    column = _CONTACT_ROLE_COLUMN.get(role)
    if column is None:
        raise ValueError(f"unknown contact role {role!r}")
    row = _settings(entity_id)
    contact_id = getattr(row, column, None) if row is not None else None
    return XeroContactSync.objects.filter(id=contact_id).first() if contact_id else None


def missing_slots(entity_id) -> list[str]:
    """Labels of the slots not mapped yet, in the dashboard's order."""
    row = _settings(entity_id)
    skip = CASH_SALE_COLUMNS if banks_all_sales(entity_id) else ()
    if minty_on_its_own(entity_id):
        slots = [(column, label) for column, label in MINTY_SLOTS if column not in skip]
        chosen = {getattr(row, column, None) if row is not None else None for column, _ in slots} - {None}
        found = set(AccountInfo.objects.filter(id__in=chosen).values_list("id", flat=True))
        return [label for column, label in slots
                if (getattr(row, column, None) if row is not None else None) not in found]
    slots = [slot for slot in PETTYCASH_SLOTS if slot[0] not in skip]
    if row is None:
        return [label for _, label, _ in slots]
    ids = {column: getattr(row, column) for column, _, _ in slots}
    accounts = set(
        AccountInfo.objects.filter(id__in=[v for c, _, k in slots if k == "account" and (v := ids[c])])
        .values_list("id", flat=True)
    )
    contacts = set(
        XeroContactSync.objects.filter(id__in=[v for c, _, k in slots if k == "contact" and (v := ids[c])])
        .values_list("id", flat=True)
    )
    return [
        label
        for column, label, kind in slots
        if not ids[column] or ids[column] not in (accounts if kind == "account" else contacts)
    ]


def settings_complete(entity_id) -> bool:
    """All six accounts and all three contacts mapped."""
    return not missing_slots(entity_id)
