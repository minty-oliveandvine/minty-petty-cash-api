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
from minty_db.enums import ReportStatus
from minty_db.models import (
    Attachment,
    Report,
    ReportExpense,
    ReportExpenseAttachment,
    XeroContactSync,
)

from core.policy import Permission, has_permission
from petty_cash.files import storage
from petty_cash.services.expenses import (
    ExpenseRefused,
    checked_receipt,
    find_account,
    find_supplier,
    replace_receipts,
)
from petty_cash.services.reports import log_history

logger = logging.getLogger("petty-cash-api")

MAX_REASON = 300
MAX_DESCRIPTION = 300
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
        old = locked.discrepancy_reason or ""
        locked.discrepancy_reason = reason
        _save(locked, "discrepancy_reason")
        if old != reason:
            log_history(locked, user, action="updated", field_changed="discrepancy_reason", old_value=old,
                        new_value=reason)
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
        old = locked.cash_addition_type or ""
        locked.cash_addition_type = kind
        _save(locked, "cash_addition_type")
        if old != kind:
            log_history(locked, user, action="updated", field_changed="cash_addition_type", old_value=old,
                        new_value=kind)
    return kind


def _line_changes(row: ReportExpense, description, supplier, account, uploaded) -> tuple[list[str], list[str]]:
    """What an edit changes on a line, as (before, after) phrases for its history row - only the
    fields that really change."""
    was, now = [], []
    if description is not None and description != (row.description or ""):
        was.append(f"description {row.description or ''}".rstrip())
        now.append(f"description {description}".rstrip())
    if supplier is not None and supplier.id != row.contact_id:
        old = XeroContactSync.objects.filter(id=row.contact_id).values_list("name", flat=True).first()
        was.append(f"supplier {old or 'none'}")
        now.append(f"supplier {supplier.name}")
    if account is not None and account.id != row.account_id:
        was.append(f"account {row.item}")
        now.append(f"account {account.name}")
    if uploaded:
        links = ReportExpenseAttachment.objects.filter(report_expense_id=row.id).order_by("sort_order")
        names = dict(Attachment.objects.filter(id__in=links.values("attachment_id"))
                     .values_list("id", "original_name"))
        old = ", ".join(names.get(link.attachment_id) or "" for link in links) or "none"
        was.append(f"receipt {old}")
        now.append("receipt " + ", ".join(name for _, _, name, _ in uploaded))
    return was, now


def edit_expense_line(report: Report, line: ReportExpense, user, *, description=None, supplier_id=None,
                      account_id=None, receipts=()) -> None:
    """Change a submitted line's details - never its amount. ``None`` leaves a field as it is;
    ``supplier_id`` / ``account_id`` are Xero ids; ``receipts`` are uploaded files that replace the
    line's receipts."""
    check_editable(report, user)
    entity_id = report.entity_id

    if description is not None:
        description = str(description).strip()
        if len(description) > MAX_DESCRIPTION:
            raise EditRefused(f"Keep the description to {MAX_DESCRIPTION} characters or fewer.", 422)
    supplier = account = None
    if supplier_id is not None:
        supplier = find_supplier(entity_id, supplier_id)
        if supplier is None:
            raise EditRefused("Choose a supplier from the list.", 422)
    if account_id is not None:
        account = find_account(entity_id, account_id)
        if account is None:
            raise EditRefused("Choose an account code from the list.", 422)
    checked = []
    for upload in receipts:
        try:
            checked.append(checked_receipt(upload))
        except ExpenseRefused as refused:
            raise EditRefused(refused.message, 422) from None

    item = account.name if account is not None else line.item
    label = item or (description if description is not None else line.description) or "EXPENSE"
    uploaded = []  # (key, stored mime, original name, stored size)
    try:
        for i, (data, mime, name) in enumerate(checked):
            key, stored_mime, stored_size = storage.upload_receipt(
                data, mime, report, filename=name, description=label, amount=line.amount,
                file_index=i if len(checked) > 1 else None,
            )
            uploaded.append((key, stored_mime, name, stored_size))
        with transaction.atomic():
            locked = _locked(report, user)
            row = ReportExpense.objects.select_for_update().get(id=line.id)
            was, now = _line_changes(row, description, supplier, account, uploaded)
            old_item = row.item
            fields = []
            if description is not None:
                row.description = description
                fields.append("description")
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
            if was:
                log_history(locked, user, action="updated", field_changed=f"expense_{row.id}",
                            old_value=f"{old_item}: {', '.join(was)}", new_value=f"{row.item}: {', '.join(now)}")
    except BaseException:
        storage.delete_files([key for key, *_ in uploaded])
        raise

