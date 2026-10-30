"""Report history's movements CSV - Flask's ``blueprints/report/services/report_csv.py``, ported
byte for byte: one line per money movement of each submitted report in a range, in the columns a
bank-statement import reads. Database only.

Per report, in this order: the cash added at the start (the owner's account when it came from the
owner, else the petty-cash account), each expense line as entered (its own account code, or
``N/A``), the cash sales, the bank deposit (the petty-cash account) and the cash difference
(negative when short). Amounts are written as Python floats (``300.0``, ``-25.1``) - Flask's
``Money`` columns read as floats, so the Decimals here go through ``float`` to write the same text.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from datetime import date
from urllib.parse import quote

from minty_db.enums import DiscrepancyType, ReportStatus
from minty_db.models import AccountInfo, EntityPettycashSettings, Report, ReportExpense

from core.policy import Permission, has_permission

COLUMNS = ("Date", "Account Code", "Amount", "Description", "Reference", "Check Number")
#: The longest range one export covers, in days counted inclusively (a leap year).
MAX_DAYS = 366
LINE_END = "\n"
#: The account roles the rows book against (``entity_pettycash_settings`` columns).
_ROLES = {
    "pettycash": "pettycash_account_id",
    "director": "director_account_id",
    "cash_sale": "cash_sale_account_id",
    "discrepancy_account": "discrepancy_account_id",
}
#: Flask's ``blueprints/shared/entity_ref.py``: a company's name as an address segment.
SLUG_MAX = 60
EMPTY_SLUG = "company"


class RangeRefused(ValueError):
    """A range the export refuses: ``reason`` is ``missing``, ``format``, ``order`` or ``too_long``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def parse_range(start: str | None, end: str | None) -> tuple[date, date]:
    """Both dates, ``YYYY-MM-DD``, start on or before end, at most ``MAX_DAYS`` days."""
    if not start or not end:
        raise RangeRefused("missing")
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise RangeRefused("format") from None
    if first > last:
        raise RangeRefused("order")
    if (last - first).days + 1 > MAX_DAYS:
        raise RangeRefused("too_long")
    return first, last


def may_export(user, entity_id) -> bool:
    """The range spans everyone's reports, so it takes the company-wide view."""
    return has_permission(user, Permission.REPORT_VIEW_ENTITY, entity_id)


def submitted_reports(entity_id, first: date, last: date) -> list[Report]:
    """The company's submitted and published reports in the range, by day (never a draft)."""
    return list(
        Report.objects.filter(entity_id=entity_id, transaction_date__gte=first, transaction_date__lte=last)
        .exclude(status=ReportStatus.DRAFT)
        .order_by("transaction_date")
    )


def _codes(entity_id) -> dict:
    """Each configured role's account code: ``""`` when the account has no code, absent when the
    role has no account."""
    settings = EntityPettycashSettings.objects.filter(entity_id=entity_id).first()
    if settings is None:
        return {}
    ids = {role: str(getattr(settings, column)) for role, column in _ROLES.items() if getattr(settings, column)}
    accounts = {str(a.id): a for a in AccountInfo.objects.filter(id__in=list(ids.values()))} if ids else {}
    return {role: accounts[i].xero_code or "" for role, i in ids.items() if i in accounts}


def _lines(report_ids) -> dict:
    """Each report's expense lines as entered, with their account's code."""
    out: dict[str, list] = {}
    if not report_ids:
        return out
    rows = list(ReportExpense.objects.filter(report_id__in=report_ids).order_by("created_at", "id"))
    account_ids = {str(r.account_id) for r in rows if r.account_id}
    codes = {str(a.id): a.xero_code for a in AccountInfo.objects.filter(id__in=account_ids)} if account_ids else {}
    for line in rows:
        line.account_code = codes.get(str(line.account_id)) if line.account_id else None
        out.setdefault(str(line.report_id), []).append(line)
    return out


def _amount(value) -> str:
    return str(float(value))


def movements(entity_id, reports) -> list[list[str]]:
    """The CSV's rows (without the header) for ``reports``, all of ``entity_id``."""
    codes = _codes(entity_id)
    petty = codes.get("pettycash")
    lines = _lines([str(r.id) for r in reports])
    rows: list[list[str]] = []
    for report in reports:
        day = report.transaction_date.strftime("%Y-%m-%d")

        def row(code, amount, description, day=day):
            rows.append([day, code, _amount(amount), description, " ", " "])

        if report.cash_addition:
            from_owner = report.cash_addition_type == "personal" and "director" in codes
            row((codes["director"] if from_owner else petty) or "", report.cash_addition,
                "Cash Addition/Withdrawal From (Start)")
        for line in lines.get(str(report.id), []):
            remarks = line.remarks
            row(line.account_code or "N/A", -(line.amount or 0),
                "Expense" if remarks in ("No description", "-") else remarks or "Expense")
        if report.cashsale_total and report.cashsale_total > 0:
            row(codes.get("cash_sale", ""), report.cashsale_total, "Cash Sale (Pettycash)")
        if report.bank_deposit and report.bank_deposit > 0:
            row(petty or "", -report.bank_deposit, "Bank Deposit")
        kind, difference = str(report.discrepancy_type or ""), report.discrepancy_amount
        if kind and kind != "none" and difference:
            short = kind.strip().lower() in (DiscrepancyType.SHORT, "shortage")
            row(codes.get("discrepancy_account") or "", -abs(difference) if short else abs(difference),
                f"Discrepancy ({kind})")
    return rows


def to_csv(rows) -> bytes:
    """The file: the header, then ``rows``; UTF-8 without a byte-order mark."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator=LINE_END)
    writer.writerow(COLUMNS)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def slugify_name(name: str | None) -> str:
    """Flask's ``entity_ref.slugify_name``: lowercase, ``&`` as "and", letters and digits of any
    script kept, anything else a single ``-``."""
    text = unicodedata.normalize("NFKC", name or "").lower().replace("&", " and ")
    slug = "".join(ch if ch.isalnum() else "-" for ch in text)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")[:SLUG_MAX].rstrip("-")
    return slug or EMPTY_SLUG


def export_disposition(company_name: str | None, first: date, last: date) -> str:
    """The ``Content-Disposition``: the company's address name and the range. A name with letters
    outside ASCII travels as ``filename*`` (RFC 6266), with an ASCII ``filename`` beside it."""
    tail = f"_reports_{first.isoformat()}_to_{last.isoformat()}.csv"
    slug = slugify_name(company_name)
    if slug.isascii():
        return f"attachment; filename={slug}{tail}"
    plain = re.sub(r"-{2,}", "-", "".join(ch if ch.isascii() else "-" for ch in slug)).strip("-") or EMPTY_SLUG
    return f"attachment; filename={plain}{tail}; filename*=UTF-8''{quote(slug + tail, safe='')}"
