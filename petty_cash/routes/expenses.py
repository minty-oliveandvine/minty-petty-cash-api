"""The report wizard's Expenses step - Flask's ``petty_cash_api/routes/expenses.py``.

- ``GET /reports/expenses?transaction_date=``: the day's draft, its lines and total, the suppliers
  and accounts a line may use. No draft is 409 ``{error, to: "opening"}``.
- ``POST /reports/expenses/lines`` (multipart): ``transaction_date, amount, remarks, supplier_id,
  account_id`` and one ``receipt`` - every field checked here: an amount above 0 and up to the
  maximum, a supplier and an account from the lists above, a description of at most 300
  characters, one PDF / JPEG / PNG of at most 10 MB judged by its bytes.
- ``DELETE /expense-lines/{id}``: a line of this company's draft.
- ``GET /expense-lines/{id}/receipts/{attachment id}``: the receipt's bytes - by id, never by key.
- ``PUT /reports/expenses`` ``{transaction_date, action}``: the step's Save & Next / Save & Exit.
- ``POST /suppliers`` ``{name}``: a new supplier, created in the company's Xero organisation
  (``CONTACT_CREATE``) - 201 ``{id, name}``; a refusal from the connection is 409, Flask's token
  service unreachable 503.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from ninja import Router

from core.exceptions import Refused
from core.policy import Permission, can_delete_report, can_view_report, has_permission
from petty_cash.days import parse_day
from petty_cash.files.storage import read_receipt, sniff_mime
from petty_cash.money import MAX_AMOUNT, cents, out
from petty_cash.routes._input import BAD_DATE, action_type, json_body, query_day, require_edit
from petty_cash.services.expenses import (
    ExpenseRefused,
    add_expense,
    delete_expense,
    expense_accounts,
    lines,
    save_expenses_step,
    suppliers,
)
from petty_cash.services.reports import currency_code, draft_payload, find_draft, progress_payload
from petty_cash.services.suppliers import MAX_NAME as MAX_SUPPLIER_NAME
from petty_cash.services.suppliers import SupplierRefused, create_supplier
from shared_models.models import (
    AccountInfo,
    Attachment,
    Report,
    ReportExpense,
    ReportExpenseAttachment,
    XeroContactSync,
)

router = Router()

NO_DRAFT = "I don't see a draft yet - let's start with the opening entry."
NOT_FOUND = "Hmm, I couldn't find that expense."
NO_FILE = "I couldn't find that file."
MAX_REMARKS = 300


def _receipt_name(attachment: Attachment) -> str:
    return attachment.original_name or os.path.basename(attachment.file_path or "") or "Receipt"


def lines_payload(rows) -> list[dict]:
    """Each line as the page reads it - the suppliers, accounts and receipts loaded in bulk."""
    rows = list(rows)
    contacts = {c.id: c for c in XeroContactSync.objects.filter(id__in=[r.contact_id for r in rows if r.contact_id])}
    accounts = {a.id: a for a in AccountInfo.objects.filter(id__in=[r.account_id for r in rows if r.account_id])}
    links = list(
        ReportExpenseAttachment.objects.filter(report_expense_id__in=[r.id for r in rows]).order_by("sort_order")
    )
    files = {a.id: a for a in Attachment.objects.filter(id__in=[link.attachment_id for link in links])}
    receipts: dict[str, list] = {}
    for link in links:
        attachment = files.get(link.attachment_id)
        if attachment is not None:
            receipts.setdefault(link.report_expense_id, []).append(
                {"id": attachment.id, "name": _receipt_name(attachment), "mime_type": attachment.mime_type}
            )
    payload = []
    for row in rows:
        contact, account = contacts.get(row.contact_id), accounts.get(row.account_id)
        payload.append(
            {
                "id": str(row.id),
                "item": row.item or "",
                "amount": out(row.amount),
                "remarks": row.remarks or "",
                "supplier": {"id": contact.xero_contact_id, "name": contact.name} if contact else None,
                "account": (
                    {"id": account.xero_account_id, "code": account.xero_code or "", "name": account.name}
                    if account
                    else None
                ),
                "receipts": receipts.get(row.id, []),
            }
        )
    return payload


@router.get("/reports/expenses")
def expenses_page(request):
    require_edit(request)
    entity_id = request.company.id
    day = query_day(request, required=True)
    draft = find_draft(entity_id, day)
    if draft is None:
        raise Refused(NO_DRAFT, 409, to="opening")
    rows = list(lines(draft.id))
    return {
        "transaction_date": day.isoformat(),
        "currency_code": currency_code(entity_id),
        "draft": draft_payload(draft),
        "lines": lines_payload(rows),
        "total": out(sum((cents(r.amount) for r in rows), Decimal("0.00"))),
        "suppliers": [{"id": c.xero_contact_id, "name": c.name} for c in suppliers(entity_id)],
        "accounts": [{"id": a.xero_account_id, "code": a.xero_code or "", "name": a.name}
                     for a in expense_accounts(entity_id)],
        "permissions": {"create_supplier": has_permission(request.auth_user, Permission.CONTACT_CREATE, entity_id)},
    }


def _amount(raw) -> Decimal | None:
    """A typed amount (commas allowed), above 0 and up to the maximum."""
    text = str(raw or "").replace(",", "").strip()
    try:
        value = cents(Decimal(text))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() and 0 < value <= MAX_AMOUNT else None


def _receipt(request) -> tuple[bytes, str, str]:
    """The uploaded receipt as ``(bytes, mime judged by the bytes, original name)``."""
    files = [f for f in request.FILES.getlist("receipt") if f and (f.name or "").strip()]
    if not files:
        raise Refused("Attach the receipt to add this expense.", 422)
    if len(files) > 1:
        raise Refused("Attach one receipt per expense.", 422)
    upload = files[0]
    if upload.size == 0:
        raise Refused("That file is empty. Attach the receipt again.", 422)
    if upload.size > settings.MAX_RECEIPT_BYTES:
        raise Refused("Attach a file of 10 MB or less.", 422)
    data = upload.read()
    mime = sniff_mime(data[:16])
    if mime is None:
        raise Refused("Attach a PDF, JPEG or PNG file.", 422)
    return data, mime, upload.name


@router.post("/reports/expenses/lines")
def add_expense_line(request):
    require_edit(request)
    entity_id = request.company.id
    form = request.POST
    day = parse_day(form.get("transaction_date"))
    if day is None:
        raise Refused(BAD_DATE, 422)
    amount = _amount(form.get("amount"))
    if amount is None:
        raise Refused(f"Enter the amount as a number above 0 and up to {MAX_AMOUNT:,.2f}.", 422)
    remarks = (form.get("remarks") or "").strip()
    if len(remarks) > MAX_REMARKS:
        raise Refused(f"Keep the description to {MAX_REMARKS} characters or fewer.", 422)
    supplier = suppliers(entity_id).filter(xero_contact_id=form.get("supplier_id") or "").first()
    if supplier is None:
        raise Refused("Choose a supplier from the list.", 422)
    account = expense_accounts(entity_id).filter(xero_account_id=form.get("account_id") or "").first()
    if account is None:
        raise Refused("Choose an account code from the list.", 422)
    data, mime, name = _receipt(request)

    draft = find_draft(entity_id, day)
    if draft is None:
        raise Refused(NO_DRAFT, 409, to="opening")
    try:
        line = add_expense(draft, request.auth_user, amount=amount, remarks=remarks, supplier=supplier,
                           account=account, receipt=data, receipt_mime=mime, receipt_name=name)
    except ExpenseRefused as refused:
        raise Refused(refused.message, 409) from None
    return JsonResponse({"line": lines_payload([line])[0], "total": out(draft.expense_total)}, status=201)


def _company_line(entity_id, line_id) -> tuple[ReportExpense | None, Report | None]:
    """The line and its report, when the line is on a report of this company."""
    try:
        line_id = str(uuid.UUID(str(line_id)))
    except ValueError:
        return None, None
    line = ReportExpense.objects.filter(id=line_id).first()
    report = Report.objects.filter(id=line.report_id).first() if line is not None else None
    if report is None or str(report.entity_id) != str(entity_id):
        return None, None
    return line, report


@router.delete("/expense-lines/{line_id}")
def delete_expense_line(request, line_id: str):
    line, report = _company_line(request.company.id, line_id)
    if line is None:
        raise Refused(NOT_FOUND, 404)
    if not can_delete_report(request.auth_user, report):
        raise Refused("It looks like you don't have permission to delete this expense.", 403)
    try:
        total = delete_expense(report, line, request.auth_user)
    except ExpenseRefused as refused:
        raise Refused(refused.message, 409) from None
    return {"total": out(total)}


@router.get("/expense-lines/{line_id}/receipts/{attachment_id}")
def expense_receipt(request, line_id: str, attachment_id: str):
    line, report = _company_line(request.company.id, line_id)
    if line is None or not can_view_report(request.auth_user, report):
        raise Refused(NO_FILE, 404)
    try:
        attachment_id = str(uuid.UUID(str(attachment_id)))
    except ValueError:
        raise Refused(NO_FILE, 404) from None
    if not ReportExpenseAttachment.objects.filter(report_expense_id=line.id, attachment_id=attachment_id).exists():
        raise Refused(NO_FILE, 404)
    attachment = Attachment.objects.filter(id=attachment_id).first()
    if attachment is None or not attachment.file_path:
        raise Refused(NO_FILE, 404)
    body = read_receipt(attachment.file_path, settings.MAX_RECEIPT_BYTES)
    if len(body) > settings.MAX_RECEIPT_BYTES:
        raise Refused("That file is too big to show here.", 413)
    resp = HttpResponse(body, content_type=sniff_mime(body[:16]) or attachment.mime_type or "application/octet-stream")
    resp["Content-Disposition"] = "inline"
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


@router.put("/reports/expenses")
def save_expenses_step_route(request):
    require_edit(request)
    entity_id = request.company.id
    body = json_body(request)
    if not isinstance(body, dict):
        raise Refused("Send the step as JSON.", 422)
    day = parse_day(body.get("transaction_date"))
    if day is None:
        raise Refused(BAD_DATE, 422)
    action = action_type(body)
    if action is None:
        raise Refused("Choose Save & Next or Save & Exit.", 422)
    draft = find_draft(entity_id, day)
    if draft is None:
        raise Refused(NO_DRAFT, 409, to="opening")
    # Flask's Save & Exit records nothing (the page leaves without calling the server).
    if action == "save_next":
        draft = save_expenses_step(draft, "save_next")
    return progress_payload(draft, "deposit" if action == "save_next" else "dashboard")


@router.post("/suppliers")
def add_supplier(request):
    """``{name}`` -> a new contact in the company's Xero organisation, kept as a synced supplier
    (``services/suppliers.py``)."""
    entity_id = request.company.id
    if not has_permission(request.auth_user, Permission.CONTACT_CREATE, entity_id):
        raise Refused("You do not have permission to create contacts for this entity.", 403)
    body = json_body(request)
    name = (body.get("name") if isinstance(body, dict) else None) or ""
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        raise Refused("The supplier's name is still empty. Add the name to continue.", 422)
    if len(name) > MAX_SUPPLIER_NAME:
        raise Refused(f"Keep the supplier's name to {MAX_SUPPLIER_NAME} characters or fewer.", 422)
    try:
        row = create_supplier(entity_id, name)
    except SupplierRefused as refused:
        raise Refused(refused.message, refused.status) from None
    return JsonResponse({"id": row.xero_contact_id, "name": row.name}, status=201)
