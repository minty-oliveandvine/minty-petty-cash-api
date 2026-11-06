"""The dashboard's "Before You Start" correction of the previous day's bank deposit.

Ported from Flask in Phase 3 of the Flask retirement (2026-11-05). It replaces the call to Flask's
``POST /api/internal/xero/deposit-correction``. The Flask sources:
- ``petty_cash_api/routes/home.py``: ``_valid_correction``, ``correct_deposit``;
- ``report/routes/legacy.py``: ``correct_previous_deposit``;
- ``xero/services/publish.py``: ``update_after_deposit_change``, ``find_xero_bank_transfer_for_deposit``,
  ``update_xero_deposit_after_change``;
- ``report/services/shared.py``: ``update_report_after_deposit_change``, ``recalculate_report``,
  ``propagate_opening_balance_to_next_day_draft``.

The deposit can only go down (or to none), and only when the deposit and petty cash accounts are
mapped. The report and the next day's draft are recalculated and saved first, in one transaction.
Then, when the report is in Xero, Xero is brought into line. Xero's bank transfers can't be
edited, so a reversal is posted and, for a smaller deposit, a replacement: the ledger keeps a trail
of the correction. A Xero failure after the save is a ``warning``: the report is corrected and the
person is told to check Xero.

Deliberate differences from Flask:
- Money is exact (Decimal), never float.
- Transfers are in the company's currency (owner, 2026-11-06); Flask always sent HKD.
- A message to the person never carries Xero's raw text or an exception's; those are logged.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from decimal import Decimal

import requests
from django.db import transaction
from minty_db.enums import SaleType
from minty_db.models import Entity, Report, ReportSale, SaleInfo

from core.xero_tokens import XeroTokenUnavailable, xero_token
from petty_cash.money import cents, out
from petty_cash.services.reports import currency_code
from petty_cash.services.sales import CASH_VALUE_NAME, cash_sales_from_detail
from petty_cash.services.xero_settings import account_for_role
from petty_cash.xero import client, publish_record

logger = logging.getLogger("petty-cash-api")

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: Two amounts in Xero are the same deposit when they differ by less than half a cent.
_SAME_AMOUNT = Decimal("0.005")

LARGER = "That deposit is larger than the previous one — could you double-check the amount?"
FAILED = "Something went wrong on my end while checking that deposit. Mind trying again?"


# ---- the request -----------------------------------------------------------------------------


def valid_correction(body) -> tuple[dict | None, str | None]:
    """The correction the body asks for, or the sentence saying what is wrong with it."""
    if not isinstance(body, dict):
        return None, "Send the correction as JSON."
    kind = body.get("kind")
    if kind not in ("change", "no"):
        return None, "Choose whether the deposit changed or there was no deposit."
    day = body.get("date")
    if not isinstance(day, str) or not _ISO_DATE.match(day):
        return None, "The report date is missing. Reload the page and try again."
    try:
        date.fromisoformat(day)
    except ValueError:
        return None, "The report date is missing. Reload the page and try again."
    amount = body.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 0:
        return None, "Enter the amount deposited, 0 or more."
    if kind == "no" and amount != 0:
        return None, "No deposit means an amount of 0."
    return {"kind": kind, "date": day, "amount": cents(amount)}, None


# ---- the report ------------------------------------------------------------------------------


def _sales_by_type(report: Report) -> dict[str, Decimal]:
    """``{electronic, delivery, cash}`` from the report's sale rows (Flask's ``sum_sales_by_type``):
    Cash by its catalogue row, ``delivery`` by type, every other typed method as electronic. A row
    whose method is gone counts nowhere."""
    totals = {"electronic": Decimal("0.00"), "delivery": Decimal("0.00"), "cash": Decimal("0.00")}
    rows = ReportSale.objects.filter(report_id=report.id).values_list("amount", "sale_id")
    methods = {m.id: m for m in SaleInfo.objects.filter(id__in=[sale_id for _, sale_id in rows])}
    for amount, sale_id in rows:
        method = methods.get(sale_id)
        if method is not None and method.value_name == CASH_VALUE_NAME:
            totals["cash"] += cents(amount)
        elif method is None or not method.type:
            continue
        elif method.type == SaleType.DELIVERY:
            totals["delivery"] += cents(amount)
        else:
            totals["electronic"] += cents(amount)
    return totals


def recalculate(report: Report) -> None:
    """The report's sales totals and closing balance from its rows (Flask's ``recalculate_report``):
    closing = opening + cash added + cash sales - expenses - deposit + the counted difference."""
    totals = _sales_by_type(report)
    cash = totals["cash"] or cents(report.cashsale_total)
    report.cashsale_total = cash
    report.nocashsale_total = totals["electronic"] + totals["delivery"]
    report.total_sales = cash + report.nocashsale_total
    report.closing_balance = (
        cents(report.opening_balance) + cents(report.cash_addition) + cash_sales_from_detail(report)
        - cents(report.expense_total) - cents(report.bank_deposit)
    ) + cents(report.discrepancy_amount)


def _carry_to_next_day_draft(report: Report) -> None:
    """The next day's draft opens with this report's new closing balance (submitted days keep
    the balance they were submitted with)."""
    if report.closing_balance is None:
        return
    draft = (Report.objects.select_for_update()
             .filter(entity_id=report.entity_id, transaction_date=report.transaction_date + timedelta(days=1),
                     status="draft")
             .order_by("-created_at").first())
    if draft is None:
        return
    draft.opening_balance = report.closing_balance
    draft.adjusted_opening_balance = cents(draft.opening_balance) + cents(draft.cash_addition)
    recalculate(draft)
    draft.save()


@transaction.atomic
def _apply(report_id, amount: Decimal) -> Report:
    report = Report.objects.select_for_update().get(id=report_id)
    report.bank_deposit = amount
    recalculate(report)
    report.save()
    _carry_to_next_day_draft(report)
    return report


# ---- Xero ------------------------------------------------------------------------------------


def _find_deposit_transfer(token, day: date, amount: Decimal) -> tuple[str | None, str | None]:
    """The BankTransferID the publish posted for this day's deposit, or ``(None, sentence)``.
    Matched by its reference ``MT{yyyymmdd}Deposit`` and amount, else by amount alone."""
    following = day + timedelta(days=1)
    where = (f"Date>=DateTime({day.year},{day.month},{day.day})"
             f"&&Date<DateTime({following.year},{following.month},{following.day})")
    try:
        response = client.get("BankTransfers", token.access_token, token.xero_org_id, params={"where": where})
    except requests.RequestException as exc:
        logger.error("Deposit correction: Xero unreachable looking up the deposit: %s", exc)
        return None, "Xero could not be reached to find the original deposit"
    if response.status_code not in (200, 201):
        logger.warning("Deposit correction: GET /BankTransfers answered %s (where %r): %s",
                       response.status_code, where, (response.text or "")[:500])
        return None, "Xero refused the look-up of the original deposit"
    try:
        transfers = (response.json() or {}).get("BankTransfers") or []
    except ValueError:
        logger.error("Deposit correction: Xero's BankTransfers answer was not JSON")
        return None, "Xero's answer about the original deposit could not be read"

    reference = f"MT{day.strftime('%Y%m%d')}Deposit"

    def amount_of(transfer) -> Decimal | None:
        try:
            return Decimal(str(transfer.get("Amount") or 0))
        except (ArithmeticError, ValueError):
            return None

    for by_reference in (True, False):
        for transfer in transfers:
            found = amount_of(transfer)
            if found is None or abs(found - amount) >= _SAME_AMOUNT:
                continue
            if by_reference and transfer.get("Reference") != reference:
                continue
            return transfer.get("BankTransferID"), None
    logger.warning("Deposit correction: no Xero transfer matched %s for %s (reference %s); saw %s",
                   day, amount, reference,
                   [{k: t.get(k) for k in ("BankTransferID", "Reference", "Date", "Amount")} for t in transfers[:10]])
    return None, (f"No BankTransfer in Xero matched date {day.isoformat()} and amount {amount.normalize():f} "
                  f"(saw {len(transfers)} same-day transfer(s))")


def _post_transfer(token, day: date, from_account: str, to_account: str, amount: Decimal, reference: str,
                   currency: str) -> str | bool:
    """Post one bank transfer; its BankTransferID (or True when Xero gave none), False when refused.
    In the company's currency (owner, 2026-11-06: Flask sent HKD whatever the company's)."""
    payload = {"BankTransfers": [{
        "FromBankAccount": {"AccountID": from_account, "CurrencyCode": currency},
        "ToBankAccount": {"AccountID": to_account, "CurrencyCode": currency},
        "Date": day.isoformat(),
        "Amount": float(amount),
        "Reference": reference,
    }]}
    try:
        response = client.post("BankTransfers", token.access_token, token.xero_org_id, payload)
    except requests.RequestException as exc:
        logger.error("Deposit correction: Xero unreachable posting %s: %s", reference, exc)
        return False
    if response.status_code != 200:
        logger.error("Deposit correction: Xero refused %s: %s %s", reference, response.status_code,
                     (response.text or "")[:500])
        return False
    return client.parse_id(response.text, "BankTransfers", "BankTransferID") or True


def correct_in_xero(entity_id, report: Report, previous: Decimal, new: Decimal) -> tuple[bool, str | None]:
    """Bring Xero's deposit for a published report into line: ``(ok, sentence when not)``."""
    if previous <= 0:
        return True, None
    try:
        token = xero_token(entity_id)
    except XeroTokenUnavailable as unavailable:
        return False, ("This entity is not connected to Xero" if unavailable.reconnect
                       else "Xero could not be reached")
    day = report.transaction_date
    transfer_id, problem = _find_deposit_transfer(token, day, previous)
    if not transfer_id:
        return False, problem or "Original deposit transfer not found in Xero"

    petty = (account_for_role(entity_id, "pettycash") or {}).get("xero_account_id")
    bank = (account_for_role(entity_id, "bank") or {}).get("xero_account_id")
    if not petty or not bank:
        return False, ("Petty cash or deposit bank account is not configured for this entity — cannot post "
                       "deposit correction to Xero")

    currency = currency_code(entity_id)
    if not currency:
        # Never a silent HKD: a company with no currency is held at Setup Required (the money rule).
        logger.error("Deposit correction: company %s has no currency; nothing posted to Xero", entity_id)
        return False, "This company has no currency chosen, so nothing was posted to Xero"

    stamp = day.strftime("%Y%m%d")
    if not _post_transfer(token, day, bank, petty, previous, f"MT{stamp}DepositReversal", currency):
        return False, ("Could not post reversal BankTransfer in Xero. The original deposit is still in Xero — "
                       "check server logs for details.")
    # The original is neutralised: a later republish must not delete it and strand the reversal.
    publish_record.forget_object(report.id, "deposit")
    if new <= 0:
        return True, None

    replacement = _post_transfer(token, day, petty, bank, new, f"MT{stamp}DepositUpdated", currency)
    if not replacement:
        return False, ("Reversal was posted in Xero, but the replacement deposit BankTransfer could not be created. "
                       "Check server logs for details.")
    # So a later republish edits THIS transfer rather than creating a third.
    org = Entity.objects.filter(id=entity_id).values_list("xero_org_id", flat=True).first()
    publish_record.record_object(report.id, org or "", "deposit", replacement, object_type="BANK_TRANSFER",
                                 amount=float(new))
    return True, None


# ---- the answer ------------------------------------------------------------------------------


def correct_deposit(entity_id, correction: dict) -> tuple[dict, int]:
    """The correction's answer ``(json, status)``; the person's right was checked by the route.
    Flask's ``correct_previous_deposit`` and ``correct_deposit``, one answer."""
    day = date.fromisoformat(correction["date"])
    amount: Decimal = correction["amount"]
    next_day = (day + timedelta(days=1)).isoformat()
    try:
        report = Report.objects.filter(entity_id=entity_id, transaction_date=day).first()
        if report is None:
            return {"status": "warning", "message": "No previous report found to update.",
                    "next_transaction_date": next_day}, 200

        previous = cents(report.bank_deposit)
        published = report.publishing_status == "completed"
        if previous > 0:
            missing = [label for role, label in (("bank", "Deposit Bank Account"), ("pettycash", "Petty Cash Account"))
                       if not account_for_role(entity_id, role)]
            if missing:
                return {"error": ("Cannot change or remove this deposit because the following entity setting(s) "
                                  f"are not configured: {', '.join(missing)}. Configure them in Settings → Petty "
                                  "Cash before correcting this deposit.")}, 409
        if amount > previous:
            return {"error": LARGER}, 400

        if report.status == "draft":
            # Flask's correction only ever wrote a submitted report and answered success without
            # touching a draft; say so in the log rather than pretend.
            logger.warning("Deposit correction for company %s on %s found only a draft; nothing changed",
                           entity_id, day)
            updated = report
        else:
            updated = _apply(report.id, amount)
        logger.info("Deposit correction: report %s deposit %s -> %s", updated.id, previous, amount)

        xero_status, xero_message = "skipped", None
        if published and previous > 0:
            try:
                ok, detail = correct_in_xero(entity_id, updated, previous, amount)
            except Exception:  # the report is already corrected: say Xero failed, never a 500
                logger.exception("Deposit correction: the Xero update failed for report %s", updated.id)
                ok, detail = False, "Database updated but the Xero update failed."
            xero_status = "success" if ok else "failed"
            if not ok:
                xero_message = detail or ("Database updated but Xero deposit could not be reversed/updated. "
                                          "Please verify in Xero.")
    except Exception:
        logger.exception("Deposit correction failed for company %s on %s", entity_id, day)
        return {"error": FAILED}, 500

    status, message = "success", "Previous day's deposit updated successfully."
    if xero_status == "failed":
        status = "warning"
        message = f"Local report updated, but Xero sync failed: {xero_message}"
    return {
        "status": status,
        "message": message,
        "id": str(updated.id),
        "previous_deposit": out(previous),
        "bank_deposit": out(amount),
        "closing_balance": out(updated.closing_balance),
        "is_published": published,
        "xero_status": xero_status,
        "next_transaction_date": next_day,
    }, 200
