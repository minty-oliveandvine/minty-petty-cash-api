"""Editing a SUBMITTED report without touching its balances - the difference reason, where an
addition to the drawer came from, and an expense line's details and receipt - Flask's
``blueprints/report/services/report_edits.py``, ported.

Flask's rules: ``REPORT_EDIT_OWN``; any submitted report may be edited (not only the latest - none
of this moves a balance); a draft or a report Xero is publishing is refused (409); a difference
keeps its reason, and a reason over 300 characters is refused rather than cut; a line's supplier
and account must be the company's own (its synced suppliers, its accounts switched on for Petty
Cash); a new account renames the line (its item is its account's name); the amount never changes;
a new receipt passes the Expenses step's checks and replaces the line's receipts, the replaced
files leaving the bucket only after the commit and only when no other link still uses them. Every
edit takes the report out of Xero (``mark_diverged_from_xero``) in the same transaction.

One order differs, as in ``expenses.add_expense``: the new receipt is uploaded before the rows are
written, and deleted again if they then fail (Flask left it in the bucket).
"""

from __future__ import annotations

import logging

from django.db import transaction

from core.policy import Permission, has_permission
from petty_cash.files import storage
from petty_cash.services.expenses import (
    ExpenseRefused,
    checked_receipt,
    expense_accounts,
    replace_receipts,
    suppliers,
)
from shared_models.enums import ReportStatus
from shared_models.models import Report, ReportExpense

logger = logging.getLogger("petty-cash-api")

MAX_REASON = 300
MAX_REMARKS = 300
WITHDRAWAL_TYPES = ("personal", "company")
NO_RIGHT = "It looks like you don't have permission to edit this report."
DRAFT = "This report is still a draft. Change it in the Daily Closing."
PUBLISHING = "Xero is still publishing this report. Wait until it finishes, then edit it."


class EditRefused(Exception):
    """An edit the rules refuse: the sentence to show and its HTTP status."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def may_edit_details(report: Report, user) -> bool:
    """``check_editable`` as a yes/no - the Daily Report's ``permissions.edit_details``."""
    return (report.status != ReportStatus.DRAFT and report.publishing_status != "publishing"
            and has_permission(user, Permission.REPORT_EDIT_OWN, report.entity_id))


def check_editable(report: Report, user) -> None:
    if not has_permission(user, Permission.REPORT_EDIT_OWN, report.entity_id):
        raise EditRefused(NO_RIGHT, 403)
    if report.status == ReportStatus.DRAFT:
        raise EditRefused(DRAFT, 409)
    if report.publishing_status == "publishing":
        raise EditRefused(PUBLISHING, 409)


def mark_diverged_from_xero(report: Report) -> None:
    """Flask's ``shared.mark_diverged_from_xero``: the report no longer matches what Xero holds, so
    it goes back to ``submitted`` with ``xero_integrated`` off; ``publishing_status`` and
    ``published_at`` stay, so a republish can warn that Xero already holds a copy."""
    report.xero_integrated = False
    if report.status == ReportStatus.PUBLISHED:
        report.status = ReportStatus.SUBMITTED


def _locked(report: Report, user) -> Report:
    """The report under a row lock, checked again (inside ``transaction.atomic``)."""
    locked = Report.objects.select_for_update().get(id=report.id)
    check_editable(locked, user)
    return locked


def _save(locked: Report, *fields: str) -> None:
    mark_diverged_from_xero(locked)
    locked.save(update_fields=[*fields, "xero_integrated", "status"])


def edit_difference_reason(report: Report, user, reason) -> str:
    check_editable(report, user)
    reason = str(reason or "").strip()
    if len(reason) > MAX_REASON:
        raise EditRefused(f"Keep the reason to {MAX_REASON} characters or fewer.", 422)
    with transaction.atomic():
        locked = _locked(report, user)
        kind = locked.discrepancy_type
        if not reason and kind in ("short", "over"):
            raise EditRefused(f"The cash was {kind}, so the reason is still needed. Add the reason to save it.", 422)
        locked.discrepancy_reason = reason
        _save(locked, "discrepancy_reason")
    return reason


def edit_withdrawal(report: Report, user, kind) -> str:
    check_editable(report, user)
    kind = str(kind or "").strip()
    if kind not in WITHDRAWAL_TYPES:
        raise EditRefused("That withdrawal type doesn't look quite right to me.", 400)
    # The bank account is not per report: a company withdrawal always comes from the main bank
    # account on entity_pettycash_settings (schema item 13).
    with transaction.atomic():
        locked = _locked(report, user)
        locked.cash_addition_type = kind
        _save(locked, "cash_addition_type")
    return kind


def edit_expense_line(report: Report, line: ReportExpense, user, *, remarks=None, supplier_id=None,
                      account_id=None, receipts=()) -> None:
    """Change a submitted line's details - never its amount. ``None`` leaves a field as it is;
    ``supplier_id`` / ``account_id`` are Xero ids; ``receipts`` are uploaded files that replace the
    line's receipts."""
    check_editable(report, user)
    entity_id = report.entity_id

    if remarks is not None:
        remarks = str(remarks).strip()
        if len(remarks) > MAX_REMARKS:
            raise EditRefused(f"Keep the description to {MAX_REMARKS} characters or fewer.", 422)
    supplier = account = None
    if supplier_id is not None:
        supplier = suppliers(entity_id).filter(xero_contact_id=supplier_id).first()
        if supplier is None:
            raise EditRefused("Choose a supplier from the list.", 422)
    if account_id is not None:
        account = expense_accounts(entity_id).filter(xero_account_id=account_id).first()
        if account is None:
            raise EditRefused("Choose an account code from the list.", 422)
    checked = []
    for upload in receipts:
        try:
            checked.append(checked_receipt(upload))
        except ExpenseRefused as refused:
            raise EditRefused(refused.message, 422) from None

    item = account.name if account is not None else line.item
    description = item or (remarks if remarks is not None else line.remarks) or "EXPENSE"
    uploaded = []  # (key, stored mime, original name)
    try:
        for i, (data, mime, name) in enumerate(checked):
            key, stored_mime = storage.upload_receipt(
                data, mime, report, filename=name, description=description, amount=line.amount,
                file_index=i if len(checked) > 1 else None,
            )
            uploaded.append((key, stored_mime, name))
        with transaction.atomic():
            locked = _locked(report, user)
            row = ReportExpense.objects.select_for_update().get(id=line.id)
            fields = []
            if remarks is not None:
                row.remarks = remarks
                fields.append("remarks")
            if supplier is not None:
                row.contact_id = supplier.id
                fields.append("contact_id")
            if account is not None:
                row.account_id, row.item = account.id, account.name
                fields += ["account_id", "item"]
            if fields:
                row.save(update_fields=fields)
            if uploaded:
                replace_receipts(row, uploaded, user)
            _save(locked)
    except BaseException:
        storage.delete_files([key for key, _, _ in uploaded])
        raise

