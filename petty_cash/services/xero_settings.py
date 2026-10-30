"""The company's Petty Cash Xero roles - Flask's ``blueprints/xero/services/settings.py``
(``get_entity_account_settings``, ``check_entity_xero_settings_complete``,
``get_missing_xero_settings_fields``).

Flask reads the ``entity_setup_summary`` view; this reads the same columns of
``entity_pettycash_settings`` and joins ``account_info`` / ``xero_contact_sync`` the way the view
does, so the answer is the view's: a slot is set when its row id is set and the row exists.
"""

from __future__ import annotations

from minty_db.models import AccountInfo, EntityPettycashSettings, XeroContactSync

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


def missing_slots(entity_id) -> list[str]:
    """Labels of the slots not mapped yet, in the dashboard's order."""
    row = _settings(entity_id)
    if row is None:
        return [label for _, label, _ in PETTYCASH_SLOTS]
    ids = {column: getattr(row, column) for column, _, _ in PETTYCASH_SLOTS}
    accounts = set(
        AccountInfo.objects.filter(id__in=[v for c, _, k in PETTYCASH_SLOTS if k == "account" and (v := ids[c])])
        .values_list("id", flat=True)
    )
    contacts = set(
        XeroContactSync.objects.filter(id__in=[v for c, _, k in PETTYCASH_SLOTS if k == "contact" and (v := ids[c])])
        .values_list("id", flat=True)
    )
    return [
        label
        for column, label, kind in PETTYCASH_SLOTS
        if not ids[column] or ids[column] not in (accounts if kind == "account" else contacts)
    ]


def settings_complete(entity_id) -> bool:
    """All six accounts and all three contacts mapped."""
    return not missing_slots(entity_id)
