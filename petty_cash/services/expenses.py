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

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.db.models.functions import Lower
from minty_db.enums import ReportStatus
from minty_db.models import (
    AccountInfo,
    Attachment,
    EntityAccountXero,
    Report,
    ReportExpense,
    ReportExpenseAttachment,
    XeroContactSync,
)

from petty_cash.files import storage
from petty_cash.money import cents
from petty_cash.services.reports import log_history, update_draft_progress
from petty_cash.services.sales import cash_sales_from_detail

logger = logging.getLogger("petty-cash-api")

NOT_A_DRAFT = "This report isn't a draft anymore, so I can't change its expenses."


class ExpenseRefused(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def checked_receipt(upload) -> tuple[bytes, str, str]:
    """One uploaded receipt checked as the Expenses step takes it (Flask's ``checked_receipt``, shared
    with the Daily Report's line edit): ``(bytes, mime judged by the bytes, original name)``, or
    ``ExpenseRefused`` with the sentence."""
    if upload.size == 0:
        raise ExpenseRefused("That file is empty. Attach the receipt again.")
    if upload.size > settings.MAX_RECEIPT_BYTES:
        raise ExpenseRefused("Attach a file of 10 MB or less.")
    data = upload.read()
    mime = storage.sniff_mime(data[:16])
    if mime is None:
        raise ExpenseRefused("Attach a PDF, JPEG or PNG file.")
    return data, mime, upload.name


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


def _drop_orphans(row_id, old_ids, keep_keys=()) -> None:
    """After a line's links changed (inside ``transaction.atomic``): the ``attachment`` rows of
    ``old_ids`` no link uses any more are deleted, and their files leave the bucket after the
    commit (a failed change keeps the old receipt AND its file)."""
    orphans = list(
        Attachment.objects.filter(id__in=old_ids).exclude(file_path__in=list(keep_keys))
        .exclude(id__in=ReportExpenseAttachment.objects.filter(attachment_id__in=old_ids).values("attachment_id"))
    )
    replaced = [a.file_path for a in orphans if a.file_path]
    Attachment.objects.filter(id__in=[a.id for a in orphans]).delete()

    def remove():
        storage.delete_files(replaced)
        if replaced:
            logger.info("Expense %s: %s replaced receipt file(s) removed", row_id, len(replaced))

    transaction.on_commit(remove)


def replace_receipts(row: ReportExpense, uploaded, user) -> None:
    """The line's receipts become ``uploaded`` (``(key, stored mime, original name)``, already in the
    bucket); an old file no other link still uses leaves the bucket after the commit. Shared by the
    draft edit (``update_expense``) and the submitted-report edit (``report_edits``)."""
    keys = [key for key, _, _ in uploaded]
    old_ids = list(ReportExpenseAttachment.objects.filter(report_expense_id=row.id)
                   .values_list("attachment_id", flat=True))
    ReportExpenseAttachment.objects.filter(report_expense_id=row.id).delete()
    for order, (key, mime, name) in enumerate(uploaded):
        attachment = _attachment_for(key, original_name=name, mime_type=mime, file_size=None, uploaded_by=user.id)
        ReportExpenseAttachment.objects.create(report_expense_id=row.id, attachment_id=attachment.id,
                                               sort_order=order, xero_attachment_id="")
    _drop_orphans(row.id, old_ids, keep_keys=keys)


def _remove_receipts(row: ReportExpense, remove: set[str]) -> None:
    """Drop the links to the attachments in ``remove``; the rest keep their order, renumbered from 0
    (Flask's ``set_receipts``)."""
    links = list(ReportExpenseAttachment.objects.filter(report_expense_id=row.id).order_by("sort_order"))
    gone = [link.attachment_id for link in links if str(link.attachment_id) in remove]
    ReportExpenseAttachment.objects.filter(report_expense_id=row.id, attachment_id__in=gone).delete()
    for order, link in enumerate(link for link in links if str(link.attachment_id) not in remove):
        if link.sort_order != order:
            ReportExpenseAttachment.objects.filter(id=link.id).update(sort_order=order)
    _drop_orphans(row.id, gone)


KEEP_A_RECEIPT = "Every expense needs its receipt. Attach a new receipt before removing the last one."


def update_expense(report: Report, line: ReportExpense, user, *, amount: Decimal, remarks: str,
                   supplier: XeroContactSync | None, account: AccountInfo | None, receipt=None,
                   remove_receipt_ids=()) -> ReportExpense:
    """Change a line of a DRAFT in place (Flask's ``update_expense``): the amount and description as
    given, ``supplier`` / ``account`` when they change (a new account renames the line), ``receipt``
    ``(bytes, mime, name)`` replacing the line's receipts, ``remove_receipt_ids`` dropping some - a
    line always keeps one. Recompute the totals and log it. The new receipt is uploaded first and
    deleted again if the rows then fail; replaced or removed files leave the bucket after the commit."""
    if report.status != ReportStatus.DRAFT:
        raise ExpenseRefused(NOT_A_DRAFT)
    amount = cents(amount)
    item = account.name if account is not None else line.item
    remove = {str(i) for i in remove_receipt_ids or ()}
    held = [str(i) for i in ReportExpenseAttachment.objects.filter(report_expense_id=line.id)
            .values_list("attachment_id", flat=True)]
    if receipt is None and not [i for i in held if i not in remove]:
        raise ExpenseRefused(KEEP_A_RECEIPT)

    uploaded = []  # (key, stored mime, original name)
    try:
        if receipt is not None:
            data, mime, name = receipt
            key, stored_mime = storage.upload_receipt(
                data, mime, report, filename=name, description=item or remarks or "EXPENSE", amount=amount,
            )
            uploaded.append((key, stored_mime, name))
        with transaction.atomic():
            locked = Report.objects.select_for_update().get(id=report.id)
            if locked.status != ReportStatus.DRAFT:
                raise ExpenseRefused(NOT_A_DRAFT)
            row = ReportExpense.objects.select_for_update().get(id=line.id)
            row.item, row.amount, row.remarks = item, amount, remarks
            fields = ["item", "amount", "remarks"]
            if supplier is not None:
                row.contact_id = supplier.id
                fields.append("contact_id")
            if account is not None:
                row.account_id = account.id
                fields.append("account_id")
            row.save(update_fields=fields)
            if uploaded:
                replace_receipts(row, uploaded, user)
            elif remove:
                _remove_receipts(row, remove)
            recompute_totals(locked)
            locked.save()
            log_history(locked, user, action="updated", field_changed="expense", old_value=f"expense_{row.id}",
                        new_value=f"updated to amount: ${float(amount)}")
    except BaseException:
        storage.delete_files([key for key, _, _ in uploaded])
        raise
    report.expense_total = locked.expense_total
    report.closing_balance = locked.closing_balance
    return row


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
