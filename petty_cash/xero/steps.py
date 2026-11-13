"""Putting one report into Xero: withdrawal, cash sales, expenses, deposit, discrepancy, then
removing what no longer applies. Flask's ``xero/services/publish.py`` (``xero_integrated_module``
and the step functions it calls), ported in Phase 3 of the Flask retirement (2026-11-05).

Each step answers ``(succeeded, failed)`` and writes its failures into ``run.pfr`` (the
``report_history`` items the publish reasons re-check). A step that succeeds records what Xero
made (``publish_record``), so a republish updates it rather than making a second one.

Differences from Flask, deliberate:
- every Xero call has a timeout (Flask's transfer and invoice posts had none);
- transfers carry the company's currency (owner, 2026-11-06), never a fixed HKD;
- a line's supplier that Xero lost is replaced for THIS company's lines only;
- money goes to Xero as the stored 2-place amounts, read exactly.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import requests
from minty_db.models import AccountInfo, Entity, Report, ReportExpense

from petty_cash.money import cents
from petty_cash.services.xero_settings import account_for_role, contact_for_role
from petty_cash.xero import client, publish_record
from petty_cash.xero import errors as pub_err
from petty_cash.xero.contacts import ensure_contact
from petty_cash.xero.errors import PublishFailureReason, translate_xero_error
from petty_cash.xero.receipts import attach_receipt

logger = logging.getLogger("petty-cash-api")

# What each company-wide step depends on (the reasons' re-check): [kind, role].
DEPS_CASH_SALES = [["contact", "cashsale_contact"], ["account", "cash_sale"]]
DEPS_DISCREPANCY = [["contact", "discrepancy_contact"], ["account", "discrepancy_account"], ["account", "pettycash"]]
DEPS_DEPOSIT = [["account", "pettycash"], ["account", "bank"]]
DEPS_WITHDRAWAL_PERSONAL = [["contact", "director_contact"], ["account", "director"], ["account", "pettycash"]]
DEPS_WITHDRAWAL_COMPANY = [["account", "bank"], ["account", "pettycash"]]
SYSTEM_ACCOUNT_CODES = {"497"}
#: Expenses go in batches of five with a short pause between, as Flask sends them.
EXPENSE_BATCH = 5
BATCH_PAUSE = 0.5


@dataclass
class Run:
    """One publish: the company, its Xero token and organisation, its currency, the reasons
    collected so far."""

    entity: Entity
    access_token: str
    org: str
    currency: str | None
    pfr: PublishFailureReason = field(default_factory=PublishFailureReason)


def _amount(value) -> float:
    """A stored amount as Xero's JSON number."""
    return float(cents(value))


def _module_error(run: Run, label: str, reason: str, meta: dict | None = None) -> None:
    meta = meta or {}
    run.pfr.add_module_error(label, reason, scope=meta.get("scope"), expense_id=meta.get("expense_id"),
                             deps=meta.get("deps"), failed_contact_id=meta.get("failed_contact_id"),
                             module=meta.get("module"))


def _existing_id(entry, expected_type: str) -> str | None:
    """A recorded id, only when it is the type about to be sent (a withdrawal can change shape)."""
    if not isinstance(entry, dict):
        return None
    if entry.get("type") and entry.get("type") != expected_type:
        return None
    return entry.get("id") or None


def _record(run: Run, report, module: str, xero_id, object_type: str, **extra) -> None:
    publish_record.record_object(report.id, run.org, module, xero_id, object_type=object_type, **extra)


# ---- the three kinds of Xero entry -----------------------------------------------------------


def bank_transaction(run: Run, day, *, kind: str, purpose: str, contact_id, bank_account_id, line_items,
                     label: str, meta: dict, subject: str = "contact", existing_id=None,
                     line: ReportExpense | None = None, contact_name: str | None = None):
    """Post (or, with ``existing_id``, update) a bank transaction. Its id (or True) or False.
    ``purpose`` names its reference: ``expense``, ``discrepancy``, else a withdrawal."""
    try:
        if contact_id:
            valid = ensure_contact(run, contact_id, contact_name)
            if not valid:
                logger.warning("Publish: contact %s could not be confirmed; posting without one", contact_id)
            contact_id = valid
        stamp = day.strftime("%Y%m%d")
        reference = {"expense": "Expense", "discrepancy": "Discrepancy"}.get(purpose, "Withdrawal")
        transaction = {"type": kind, "lineItems": line_items, "Date": day.isoformat(),
                       "bankAccount": {"accountID": bank_account_id}, "Reference": f"MT{stamp}{reference}"}
        if contact_id:
            transaction["contact"] = {"contactID": contact_id}
        if existing_id:
            transaction["bankTransactionID"] = existing_id
        path = f"BankTransactions/{existing_id}" if existing_id else "BankTransactions"
        response = client.post(path, run.access_token, run.org, {"bankTransactions": [transaction]})
    except requests.Timeout as exc:
        logger.error("Publish: Xero did not answer a bank transaction: %s", exc)
        _module_error(run, label, "Xero did not respond, please try again", meta)
        return False
    except requests.RequestException as exc:
        logger.error("Publish: Xero unreachable for a bank transaction: %s", exc)
        _module_error(run, label, "could not reach Xero, please try again", meta)
        return False
    if response.status_code != 200:
        logger.error("Publish: Xero refused a bank transaction (%s): %s %s", label, response.status_code,
                     (response.text or "")[:500])
        _module_error(run, label, translate_xero_error(response.status_code, response.text, subject=subject), meta)
        return False
    created = client.parse_id(response.text, "BankTransactions", "BankTransactionID")
    if line is not None and created:
        try:
            if not attach_receipt(run, line, created):
                logger.warning("Publish: transaction %s made, its receipt not attached", created)
        except Exception:  # the transaction stands; the receipt is logged
            logger.exception("Publish: attaching the receipt for line %s failed", line.id)
    return created or True


def _delete_transfer(run: Run, transfer_id: str) -> tuple[bool, str | None]:
    """Xero can't edit a bank transfer; it deletes one by POSTing ``{"Status": "DELETED"}``."""
    try:
        response = client.post(f"BankTransfers/{transfer_id}", run.access_token, run.org, {"Status": "DELETED"})
    except requests.RequestException as exc:
        logger.error("Publish: deleting transfer %s failed: %s", transfer_id, exc)
        return False, "could not reach Xero, please try again"
    if response.status_code == 200:
        return True, None
    logger.error("Publish: Xero refused deleting transfer %s: %s %s", transfer_id, response.status_code,
                 (response.text or "")[:300])
    return False, translate_xero_error(response.status_code, response.text, subject="account")


def bank_transfer(run: Run, report, day, *, from_account, to_account, amount, purpose: str, label: str, meta: dict,
                  existing_id=None, module: str | None = None):
    """Post a bank transfer (a recorded one is deleted first: Xero has no update). Its id or False."""
    if not run.currency:
        logger.error("Publish: company %s has no currency; transfer not posted", run.entity.id)
        _module_error(run, label, "the company's currency is not chosen", meta)
        return False
    stamp = day.strftime("%Y%m%d")
    reference = f"MT{stamp}Withdrawal" if purpose == "company" else f"MT{stamp}Deposit"
    payload = {"BankTransfers": [{
        "FromBankAccount": {"AccountID": from_account, "CurrencyCode": run.currency},
        "ToBankAccount": {"AccountID": to_account, "CurrencyCode": run.currency},
        "Date": day.isoformat(), "Amount": _amount(amount), "Reference": reference,
    }]}
    if existing_id:
        deleted, why = _delete_transfer(run, existing_id)
        # Forget BEFORE creating: a failed create then leaves no id pointing at nothing.
        if module:
            publish_record.forget_object(report.id, module)
        if not deleted:
            logger.error("Publish: transfer %s not deleted; no replacement, it would duplicate", existing_id)
            _module_error(run, label, why or "the previous transfer could not be removed from Xero", meta)
            return False
    try:
        response = client.post("BankTransfers", run.access_token, run.org, payload)
    except requests.RequestException as exc:
        logger.error("Publish: Xero unreachable for a transfer (%s): %s", label, exc)
        _module_error(run, label, "Xero rejected this entry", meta)
        return False
    if response.status_code != 200:
        logger.error("Publish: Xero refused a transfer (%s): %s %s", label, response.status_code,
                     (response.text or "")[:500])
        _module_error(run, label, translate_xero_error(response.status_code, response.text, subject="account"), meta)
        return False
    return client.parse_id(response.text, "BankTransfers", "BankTransferID") or True


def _invoice_call(run: Run, payload: dict, existing_id=None) -> requests.Response:
    """PUT /Invoices creates; POST /Invoices/{id} updates in place (a republish)."""
    if existing_id:
        return client.post(f"Invoices/{existing_id}", run.access_token, run.org, payload)
    return client.put("Invoices", run.access_token, run.org, payload)


# ---- the steps -------------------------------------------------------------------------------


def withdrawal(run: Run, report: Report, existing) -> tuple[int, int]:
    """The cash added at the start: from the director (a transaction) or the company's bank (a transfer)."""
    day = report.transaction_date
    try:
        if report.cash_addition_type == "personal":
            director = contact_for_role(run.entity.id, "director_contact")
            if director is None:
                run.pfr.add_missing(pub_err.LABEL_DIRECTOR_CONTACT, deps=[["contact", "director_contact"]],
                                    module="withdrawal_from")
                return 0, 1
            director_code = (account_for_role(run.entity.id, "director") or {}).get("account_code")
            if not director_code:
                run.pfr.add_missing(pub_err.LABEL_DIRECTOR_ACCOUNT_CODE, deps=[["account", "director"]],
                                    module="withdrawal_from")
                return 0, 1
            petty = (account_for_role(run.entity.id, "pettycash") or {}).get("xero_account_id")
            made = bank_transaction(
                run, day, kind="RECEIVE", purpose="withdrawal", contact_id=director.xero_contact_id,
                bank_account_id=petty,
                line_items=[{"description": f"Amount due to {director.name}", "quantity": 1,
                             "unitAmount": _amount(report.cash_addition), "accountCode": director_code,
                             "taxType": "NONE"}],
                label="Withdrawal", meta={"scope": "entity", "deps": DEPS_WITHDRAWAL_PERSONAL, "module": "withdrawal_from"},
                existing_id=_existing_id(existing, "BANK_TRANSACTION"),
            )
            if made:
                _record(run, report, "withdrawal_from", made, "BANK_TRANSACTION")
            return (1, 0) if made else (0, 1)
        if report.cash_addition_type == "company":
            made = bank_transfer(
                run, report, day,
                from_account=(account_for_role(run.entity.id, "bank") or {}).get("xero_account_id"),
                to_account=(account_for_role(run.entity.id, "pettycash") or {}).get("xero_account_id"),
                amount=report.cash_addition, purpose="company", label="Withdrawal",
                meta={"scope": "entity", "deps": DEPS_WITHDRAWAL_COMPANY, "module": "withdrawal_from"},
                existing_id=_existing_id(existing, "BANK_TRANSFER"), module="withdrawal_from",
            )
            if made:
                _record(run, report, "withdrawal_from", made, "BANK_TRANSFER", amount=_amount(report.cash_addition))
            return (1, 0) if made else (0, 1)
        _module_error(run, "Withdrawal", "withdrawal type is not set")
        return 0, 1
    except Exception:
        logger.exception("Publish: the withdrawal failed for report %s", report.id)
        _module_error(run, "Withdrawal", "Xero rejected this entry")
        return 0, 1


def cash_sales(run: Run, report: Report, amount, existing) -> tuple[int, int]:
    """The day's cash sales as one authorised sales invoice to the cash sale contact."""
    meta = {"scope": "entity", "deps": DEPS_CASH_SALES, "module": "invoices"}
    try:
        contact = contact_for_role(run.entity.id, "cashsale_contact")
        if contact is None:
            run.pfr.add_missing(pub_err.LABEL_CASHSALE_CONTACT, deps=[["contact", "cashsale_contact"]],
                                module="invoices")
            return 0, 1
        code = (account_for_role(run.entity.id, "cash_sale") or {}).get("account_code")
        day = report.transaction_date
        description = f"MT{day.strftime('%Y%m%d')}Cash Sales"
        payload = {"Invoices": [{
            "Type": "ACCREC", "Contact": {"ContactID": contact.xero_contact_id},
            "LineItems": [{"Description": description, "Quantity": 1, "UnitAmount": _amount(amount),
                           "AccountCode": code, "TaxType": "NONE", "LineAmount": _amount(amount)}],
            "Date": day.isoformat(), "DueDate": day.isoformat(), "Reference": description, "Status": "AUTHORISED",
        }]}
        existing_id = _existing_id(existing, "INVOICE")
        if existing_id:
            payload["Invoices"][0]["InvoiceID"] = existing_id
        try:
            response = _invoice_call(run, payload, existing_id)
        except requests.RequestException as exc:
            logger.error("Publish: Xero unreachable for the cash sales invoice: %s", exc)
            _module_error(run, "Cash sales", "could not reach Xero, please try again", meta)
            return 0, 1
        if response.status_code != 200:
            logger.error("Publish: Xero refused the cash sales invoice: %s %s", response.status_code,
                         (response.text or "")[:500])
            _module_error(run, "Cash sales",
                          translate_xero_error(response.status_code, response.text, subject="account"), meta)
            return 0, 1
        made = client.parse_id(response.text, "Invoices", "InvoiceID") or True
        _record(run, report, "invoices", made, "INVOICE")
        return 1, 0
    except Exception:
        logger.exception("Publish: the cash sales invoice failed for report %s", report.id)
        _module_error(run, "Cash sales", "Xero rejected this entry", meta)
        return 0, 1


def expenses(run: Run, report: Report, existing: dict, only_ids=None) -> tuple[int, int]:
    """One spend transaction per expense line, each with its receipt. ``only_ids``: a selective
    re-publish's lines (the ones that failed last time)."""
    try:
        lines = ReportExpense.objects.select_related("account", "contact").filter(report_id=report.id)
        if only_ids is not None:
            lines = lines.filter(id__in=[str(i) for i in only_ids])
        lines = list(lines.order_by("created_at", "id"))
        if not lines:
            if only_ids is not None:
                logger.info("Publish: the lines to retry were since deleted")
                return 0, 0
            _module_error(run, "Expenses", "expense details are missing")
            return 0, 1
        petty = (account_for_role(run.entity.id, "pettycash") or {}).get("xero_account_id")
        succeeded = 0
        for index, line in enumerate(lines):
            if index and index % EXPENSE_BATCH == 0:
                time.sleep(BATCH_PAUSE)
            contact = line.contact
            xero_contact = contact.xero_contact_id if contact else None
            label = f"Expense '{line.item}'"
            meta = {"scope": "expense", "expense_id": str(line.id), "failed_contact_id": xero_contact,
                    "module": "expenses"}
            try:
                made = bank_transaction(
                    run, report.transaction_date, kind="SPEND", purpose="expense", contact_id=xero_contact,
                    bank_account_id=petty,
                    line_items=[{"description": line.item, "unitAmount": _amount(line.amount),
                                 "accountCode": (line.account.xero_code if line.account else None) or "200",
                                 "taxType": "NONE"}],
                    label=label, meta=meta, existing_id=(existing or {}).get(str(line.id)), line=line,
                    # The name a lost supplier is made again under: the synced name, else the item.
                    contact_name=(contact.name if contact and contact.name else None) or line.item,
                )
            except Exception:
                logger.exception("Publish: expense line %s failed", line.id)
                _module_error(run, label, "Xero rejected this entry", meta)
                made = False
            if made:
                publish_record.record_object(report.id, run.org, "expenses", made, source_id=str(line.id),
                                             object_type="BANK_TRANSACTION")
                succeeded += 1
        failed = len(lines) - succeeded
        logger.info("Publish: %s/%s expense line(s) in Xero for report %s", succeeded, len(lines), report.id)
        return succeeded, failed
    except Exception:
        logger.exception("Publish: the expenses failed for report %s", report.id)
        _module_error(run, "Expenses", "Xero rejected this entry")
        return 0, 1


def deposit(run: Run, report: Report, existing) -> tuple[int, int]:
    meta = {"scope": "entity", "deps": DEPS_DEPOSIT, "module": "deposit"}
    try:
        made = bank_transfer(
            run, report, report.transaction_date,
            from_account=(account_for_role(run.entity.id, "pettycash") or {}).get("xero_account_id"),
            to_account=(account_for_role(run.entity.id, "bank") or {}).get("xero_account_id"),
            amount=report.bank_deposit, purpose="deposit", label="Deposit", meta=meta,
            existing_id=_existing_id(existing, "BANK_TRANSFER"), module="deposit",
        )
        if made:
            _record(run, report, "deposit", made, "BANK_TRANSFER", amount=_amount(report.bank_deposit))
        return (1, 0) if made else (0, 1)
    except Exception:
        logger.exception("Publish: the deposit failed for report %s", report.id)
        _module_error(run, "Deposit", "Xero rejected this entry", meta)
        return 0, 1


def _discrepancy_word(value) -> str | None:
    key = str(value or "").strip().lower()
    return {"shortage": "short", "surplus": "over"}.get(key, key) or None


def discrepancy(run: Run, report: Report, existing) -> tuple[int, int]:
    """The counted difference: a short day spends from petty cash, an over day receives into it."""
    meta = {"scope": "entity", "deps": DEPS_DISCREPANCY, "module": "discrepancy"}
    try:
        word = _discrepancy_word(report.discrepancy_type)
        if word not in ("over", "short"):
            _module_error(run, "Discrepancy", "the discrepancy type is not set")
            return 0, 1
        contact = contact_for_role(run.entity.id, "discrepancy_contact")
        if contact is None:
            _module_error(run, "Discrepancy", "discrepancy contact is not set up in Xero settings",
                          {"scope": "entity", "deps": [["contact", "discrepancy_contact"]], "module": "discrepancy"})
            return 0, 1
        made = bank_transaction(
            run, report.transaction_date, kind="SPEND" if word == "short" else "RECEIVE", purpose="discrepancy",
            contact_id=contact.xero_contact_id,
            bank_account_id=(account_for_role(run.entity.id, "pettycash") or {}).get("xero_account_id"),
            line_items=[{"description": report.discrepancy_reason, "unitAmount": abs(_amount(report.discrepancy_amount)),
                         "accountCode": (account_for_role(run.entity.id, "discrepancy_account") or {}).get("account_code"),
                         "taxType": "NONE"}],
            label="Discrepancy", meta=meta, existing_id=_existing_id(existing, "BANK_TRANSACTION"),
        )
        if made:
            _record(run, report, "discrepancy", made, "BANK_TRANSACTION")
        return (1, 0) if made else (0, 1)
    except Exception:
        logger.exception("Publish: the discrepancy failed for report %s", report.id)
        _module_error(run, "Discrepancy", "Xero rejected this entry", meta)
        return 0, 1


# ---- what no longer applies ------------------------------------------------------------------


def _delete_transaction(run: Run, xero_id) -> bool:
    if not xero_id:
        return False
    try:
        response = client.post(f"BankTransactions/{xero_id}", run.access_token, run.org,
                               {"bankTransactions": [{"bankTransactionID": xero_id, "status": "DELETED"}]})
    except requests.RequestException as exc:
        logger.error("Publish: deleting transaction %s failed: %s", xero_id, exc)
        return False
    if response.status_code == 200:
        return True
    logger.error("Publish: Xero refused deleting transaction %s: %s", xero_id, response.status_code)
    return False


def _void_invoice(run: Run, xero_id) -> bool:
    """Xero refuses to void an invoice with payments: the id is then kept, so the next run tries again."""
    if not xero_id:
        return False
    try:
        response = _invoice_call(run, {"Invoices": [{"InvoiceID": xero_id, "Status": "VOIDED"}]}, xero_id)
    except requests.RequestException as exc:
        logger.error("Publish: voiding invoice %s failed: %s", xero_id, exc)
        return False
    if response.status_code == 200:
        return True
    logger.error("Publish: Xero refused voiding invoice %s: %s", xero_id, (response.text or "")[:300])
    return False


def sweep_removed(run: Run, report: Report) -> None:
    """Remove from Xero what this report made and no longer has: a deleted line, a discrepancy, cash
    sales or a deposit gone to zero. Only ever what this report recorded. A failure here never fails
    the publish (the stale entry is the status quo), but is logged."""
    try:
        record = publish_record.load_record(report.id, run.org)
        if not record.get("objects"):
            return
        live = {str(i) for i in ReportExpense.objects.filter(report_id=report.id).values_list("id", flat=True)}
        for line_id, xero_id in publish_record.recorded_expense_ids(record).items():
            if line_id not in live and _delete_transaction(run, xero_id):
                publish_record.forget_object(report.id, "expenses", source_id=line_id)
        zeroed = []
        if not (report.discrepancy_amount and report.discrepancy_type in ("over", "short")):
            zeroed.append(("discrepancy", "BANK_TRANSACTION"))
        if not cents(report.cashsale_total) > 0:
            zeroed.append(("invoices", "INVOICE"))
        if not cents(report.bank_deposit) > 0:
            zeroed.append(("deposit", "BANK_TRANSFER"))
        for module, object_type in zeroed:
            entry = publish_record.recorded_entry(record, module)
            if not entry:
                continue
            logger.info("Publish: %s no longer applies to report %s; removing %s", module, report.id, entry["id"])
            if object_type == "BANK_TRANSACTION":
                removed = _delete_transaction(run, entry["id"])
            elif object_type == "BANK_TRANSFER":
                removed, _ = _delete_transfer(run, entry["id"])
            else:
                removed = _void_invoice(run, entry["id"])
            if removed:
                publish_record.forget_object(report.id, module)
    except Exception:
        logger.exception("Publish: removing stale Xero entries failed for report %s", report.id)


# ---- the whole report ------------------------------------------------------------------------


def whole_failure(reason: str) -> dict:
    """The result of a publish that failed as a whole (Flask's ``_aggregate_error_result``)."""
    pfr = PublishFailureReason()
    if reason:
        pfr.add_module_error("Publish", reason)
    return {"modules": {m: {"status": "error"} for m in ("withdrawal_from", "deposit", "invoices", "expenses",
                                                         "discrepancy")},
            "succeeded": 0, "failed": 1, "reasons": pfr.to_reason_bullets(), "reason_items": pfr.to_reason_items()}


def publish_report(run: Run, report: Report, retry_filter: dict | None = None) -> dict:
    """Every step with an amount (only ``retry_filter``'s on a selective re-publish), then the sweep.
    ``{modules, succeeded, failed, reasons, reason_items}``. A rejected token stops the run at once
    with one reason."""
    try:
        record = publish_record.load_record(report.id, run.org)
        modules: dict[str, dict] = {}
        totals = {"succeeded": 0, "failed": 0}
        retry_modules = retry_filter.get("modules") if retry_filter else None
        retry_lines = retry_filter.get("expense_ids") if retry_filter else None

        def counted(key, counts):
            ok, bad = counts
            totals["succeeded"] += ok
            totals["failed"] += bad
            modules[key] = {"status": "error" if bad else "success"}

        steps = (
            ("withdrawal_from", cents(report.cash_addition) > 0,
             lambda: withdrawal(run, report, publish_record.recorded_entry(record, "withdrawal_from"))),
            ("invoices", cents(report.cashsale_total) > 0,
             lambda: cash_sales(run, report, report.cashsale_total, publish_record.recorded_entry(record, "invoices"))),
            ("expenses", cents(report.expense_total) > 0,
             lambda: expenses(run, report, publish_record.recorded_expense_ids(record), retry_lines)),
            ("deposit", cents(report.bank_deposit) > 0,
             lambda: deposit(run, report, publish_record.recorded_entry(record, "deposit"))),
            ("discrepancy", bool(report.discrepancy_amount) and report.discrepancy_type in ("over", "short"),
             lambda: discrepancy(run, report, publish_record.recorded_entry(record, "discrepancy"))),
        )
        for key, applies, step in steps:
            if not applies:
                modules[key] = {"status": "success"}
            elif retry_modules is None or key in retry_modules:
                counted(key, step())
            if run.pfr.has_auth_expired():
                return whole_failure(pub_err.XERO_AUTH_EXPIRED)
        if retry_filter is None:
            sweep_removed(run, report)
        return {**{"modules": modules}, **totals, "reasons": run.pfr.to_reason_bullets(),
                "reason_items": run.pfr.to_reason_items()}
    except Exception:
        logger.exception("Publish: the run failed for report %s", report.id)
        return whole_failure("Xero rejected this entry")


def system_account_errors(entity_id, report_id) -> list[dict]:
    """Lines mapped to a Xero system account, which may not be published (Flask's
    ``validate_expenses_for_system_accounts``)."""
    rows = list(ReportExpense.objects.filter(report_id=report_id).values_list("id", "account_id"))
    accounts = {a.id: a for a in AccountInfo.objects.filter(id__in=[a for _, a in rows if a], entity_id=entity_id)}
    errors = []
    for line_id, account_id in rows:
        account = accounts.get(account_id)
        if account and str(account.xero_code or "").strip() in SYSTEM_ACCOUNT_CODES:
            errors.append({"expense": account.name or f"Expense {line_id}", "account_code": account.xero_code,
                           "message": "System accounts are not allowed to be published to Xero."})
    return errors
