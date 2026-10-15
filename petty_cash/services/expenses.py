"""The report wizard's Expenses step - Flask's ``blueprints/report/services/expenses.py``, ported.

Flask's rules: a line's item is the chosen account's name; it carries one receipt; a line can be
added to, or deleted from, a DRAFT only; every change recomputes ``expense_total`` and the closing
balance (``opening + addition + cash sales - expenses - deposit`` - the step formula, without the
cash count's difference); the step's Save & Next marks Expenses done, Save & Exit records nothing.

Two orders changed, deliberately, so nothing is lost on a failure: a receipt uploaded for a line
whose row then fails to save is deleted again, and a deleted line's receipts leave the bucket only
AFTER the rows are committed (Flask deleted the files first).
"""

from __future__ import annotations

import logging
import mimetypes
import os
from decimal import Decimal

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.db.models.functions import Lower

from petty_cash.files import storage
from petty_cash.money import cents
from petty_cash.services.reports import log_history, update_draft_progress
from petty_cash.services.sales import cash_sales_from_detail
from shared_models.enums import ReportStatus
from shared_models.models import (
    AccountInfo,
    Attachment,
    EntityAccountXero,
    Report,
    ReportExpense,
    ReportExpenseAttachment,
    XeroContactSync,
)

logger = logging.getLogger("petty-cash-api")

NOT_A_DRAFT = "This report isn't a draft anymore, so I can't change its expenses."


class ExpenseRefused(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def suppliers(entity_id):
    """The company's synced Xero contacts, A to Z - the only suppliers a line can carry."""
    return XeroContactSync.objects.filter(entity_id=entity_id).order_by(Lower("name"))


def expense_accounts(entity_id):
    """The company's accounts switched on for Petty Cash (``entity_account_xero.is_active``)."""
    active = EntityAccountXero.objects.filter(account_id=OuterRef("id"), is_active=True)
    return AccountInfo.objects.filter(entity_id=entity_id).filter(Exists(active))


def lines(report_id):
    return ReportExpense.objects.filter(report_id=report_id).order_by("created_at")


def recompute_totals(report: Report) -> Decimal:
    """``expense_total`` = the sum of the lines, then the closing balance. Returns the total."""
    total = sum((cents(a) for a in lines(report.id).values_list("amount", flat=True)), Decimal("0.00"))
    report.expense_total = total
    report.closing_balance = (
        cents(report.opening_balance)
        + cents(report.cash_addition)
        + cash_sales_from_detail(report)
        - total
        - cents(report.bank_deposit)
    )
    return total


def _attachment_for(key: str, *, original_name: str, mime_type: str, file_size: int, uploaded_by) -> Attachment:
    """The row for a stored key - reused if one exists (Flask's ``Attachment.for_key``)."""
    found = Attachment.objects.filter(file_path=key).first()
    if found is not None:
        return found
    base = os.path.basename(key) or key
    return Attachment.objects.create(
        original_name=(original_name or base)[:255],
        stored_name=base[:255],
        file_path=key,
        file_extension=os.path.splitext(base)[1].lstrip(".").lower()[:20],
        mime_type=(mime_type or mimetypes.guess_type(base)[0] or "application/octet-stream")[:100],
        file_size=file_size,
        storage_provider="s3",
        checksum_sha256="",
        uploaded_by=uploaded_by,
        is_deleted=False,
    )


def add_expense(report: Report, user, *, amount: Decimal, remarks: str, supplier: XeroContactSync,
                account: AccountInfo, receipt: bytes, receipt_mime: str, receipt_name: str) -> ReportExpense:
    """Store one line on ``report`` with its receipt, recompute the totals, log it. The receipt is
    uploaded first; if the rows then fail, the upload is deleted again."""
    if report.status != ReportStatus.DRAFT:
        raise ExpenseRefused(NOT_A_DRAFT)
    amount = cents(amount)
    item = account.name
    key, stored_mime = storage.upload_receipt(
        receipt, receipt_mime, report, filename=receipt_name, description=item or remarks or "EXPENSE",
        amount=amount,
    )
    try:
        with transaction.atomic():
            locked = Report.objects.select_for_update().get(id=report.id)
            if locked.status != ReportStatus.DRAFT:
                raise ExpenseRefused(NOT_A_DRAFT)
            line = ReportExpense.objects.create(
                report_id=locked.id, item=item, amount=amount, remarks=remarks,
                account_id=account.id, contact_id=supplier.id,
            )
            attachment = _attachment_for(key, original_name=receipt_name, mime_type=stored_mime,
                                         file_size=None, uploaded_by=user.id)
            ReportExpenseAttachment.objects.create(report_expense_id=line.id, attachment_id=attachment.id,
                                                   sort_order=0, xero_attachment_id="")
            recompute_totals(locked)
            locked.save()
            log_history(locked, user, action="added", field_changed="expense", old_value="previous_expenses",
                        new_value=f"Added expense: {item} (${float(amount)})")
    except BaseException:
        storage.delete_files([key])
        raise
    report.expense_total = locked.expense_total
    report.closing_balance = locked.closing_balance
    return line


def delete_expense(report: Report, line: ReportExpense, user) -> Decimal:
    """Delete ``line`` and its receipts from a DRAFT, recompute, log. Returns the new total."""
    if report.status != ReportStatus.DRAFT:
        raise ExpenseRefused(NOT_A_DRAFT)
    with transaction.atomic():
        locked = Report.objects.select_for_update().get(id=report.id)
        if locked.status != ReportStatus.DRAFT:
            raise ExpenseRefused(NOT_A_DRAFT)
        links = list(ReportExpenseAttachment.objects.filter(report_expense_id=line.id))
        attachment_ids = [link.attachment_id for link in links]
        line_id = line.id
        ReportExpenseAttachment.objects.filter(report_expense_id=line_id).delete()
        ReportExpense.objects.filter(id=line_id).delete()
        # attachment rows no other line (or bill) still links
        orphans = list(
            Attachment.objects.filter(id__in=attachment_ids)
            .exclude(id__in=ReportExpenseAttachment.objects.filter(attachment_id__in=attachment_ids)
                     .values("attachment_id"))
        )
        keys = [a.file_path for a in orphans]
        Attachment.objects.filter(id__in=[a.id for a in orphans]).delete()
        total = recompute_totals(locked)
        locked.save()
        log_history(locked, user, action="deleted", field_changed="expense", old_value=f"expense_{line_id}",
                    new_value="deleted")
        transaction.on_commit(lambda: storage.delete_files(keys))
    logger.info("Expense %s deleted from report %s; total now %s", line_id, report.id, total)
    return total


@transaction.atomic
def save_expenses_step(report: Report, action_type: str) -> Report:
    """Save & Next marks Expenses done and moves the report on to the deposit."""
    locked = Report.objects.select_for_update().get(id=report.id)
    update_draft_progress(locked, action_type, "expenses", "deposit")
    locked.save()
    return locked
