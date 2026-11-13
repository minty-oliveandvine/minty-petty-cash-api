"""Report history's Petty Cash Report CSV (owner, 2026-11-13): the company's daily reports in a
range as a person reads them - each day's report, then the expenses it recorded with their account
and supplier. The movements CSV (``report_csv.py``) is the bank-statement file beside it. Database
only.

The range, the reports (submitted or published, never a draft), who may export and the file's name
follow ``report_csv.py``; with no range the file is the company's whole history
(``report_csv.every_submitted_report``). One table, by day:

* a ``Daily report`` row: its Status pill, the day's figures (opening balance, cash added, cash and
  non-cash sales, expenses, bank deposit, cash short/over - negative when short - and closing
  balance) and the first name of who submitted it;
* an ``Expense`` row per line as entered (``created_at``, then ``id``): its account (code, name),
  supplier, description and amount. A line whose account or supplier row is gone shows what was
  last published to Xero; a line with no words of its own (empty, ``No description`` or ``-``)
  shows its item - publish's rule.

Amounts are plain numbers with two decimal places; the company's currency code has its own column
(``No currency selected`` when it has none - the money rule, never a silent blank).
"""

from __future__ import annotations

from decimal import Decimal

from minty_db.enums import DiscrepancyType
from minty_db.models import AccountInfo, ReportExpense, User, XeroContactSync

from petty_cash.services.report_csv import to_csv as _to_csv
from petty_cash.services.report_page import report_status

COLUMNS = (
    "Date", "Line", "Status", "Account Code", "Account Name", "Contact", "Description", "Amount",
    "Opening Balance", "Cash Added", "Cash Sales", "Non-cash Sales", "Expenses", "Bank Deposit",
    "Cash Short/Over", "Closing Balance", "Submitted By", "Currency",
)
#: The file's name: ``<company>_petty-cash-report_<start>_to_<end>.csv``.
FILE_LABEL = "petty-cash-report"
REPORT_LINE = "Daily report"
EXPENSE_LINE = "Expense"
NO_CURRENCY = "No currency selected"
#: Publish's rule for an expense line's words: these mean the person typed nothing.
_EMPTY_DESCRIPTIONS = ("", "No description", "-")
_CENT = Decimal("0.01")


def _money(value) -> str:
    """Two decimal places; empty for no value; a zero is ``0.00``, never ``-0.00``."""
    if value is None:
        return ""
    return str(Decimal(value).quantize(_CENT) + 0)


def _difference(report) -> Decimal:
    """The counted difference: negative when short, positive when over, 0 when none."""
    kind, amount = str(report.discrepancy_type or "").strip().lower(), report.discrepancy_amount
    if not kind or kind == "none" or not amount:
        return Decimal(0)
    return -abs(amount) if kind in (DiscrepancyType.SHORT, "shortage") else abs(amount)


def _lines(report_ids) -> dict[str, list]:
    """Each report's expense lines as entered, each with ``account_pair`` (code, name) and
    ``contact_name`` - the stored rows, else what was last published."""
    out: dict[str, list] = {}
    if not report_ids:
        return out
    rows = list(ReportExpense.objects.filter(report_id__in=report_ids).order_by("created_at", "id"))
    account_ids = {r.account_id for r in rows if r.account_id}
    contact_ids = {r.contact_id for r in rows if r.contact_id}
    accounts = {a.id: ((a.xero_code or a.code or "").strip(), (a.name or "").strip())
                for a in AccountInfo.objects.filter(id__in=account_ids)} if account_ids else {}
    contacts = {c.id: (c.name or "").strip()
                for c in XeroContactSync.objects.filter(id__in=contact_ids)} if contact_ids else {}
    for line in rows:
        line.account_pair = accounts.get(line.account_id) or (
            (line.published_account_code or "").strip(), (line.published_account_name or "").strip())
        line.contact_name = contacts.get(line.contact_id) or (line.published_contact_name or "").strip()
        out.setdefault(str(line.report_id), []).append(line)
    return out


def rows(reports, currency: str | None) -> list[list[str]]:
    """The CSV's rows (without the header) for ``reports``, in their order."""
    lines = _lines([r.id for r in reports])
    people = {r.created_by for r in reports if r.created_by}
    names = dict(User.objects.filter(id__in=people).values_list("id", "first_name")) if people else {}
    code = currency or NO_CURRENCY
    out: list[list[str]] = []
    for report in reports:
        day = report.transaction_date.strftime("%Y-%m-%d")
        out.append([
            day, REPORT_LINE, report_status(report)[0], "", "", "", "", "",
            _money(report.opening_balance), _money(report.cash_addition or 0),
            _money(report.cashsale_total or 0), _money(report.nocashsale_total or 0),
            _money(report.expense_total or 0), _money(report.bank_deposit or 0),
            _money(_difference(report)), _money(report.closing_balance),
            (names.get(report.created_by) or "").strip(), code,
        ])
        for line in lines.get(str(report.id), []):
            words = (line.description or "").strip()
            if words in _EMPTY_DESCRIPTIONS:
                words = (line.item or "").strip() or EXPENSE_LINE
            code_, name = line.account_pair
            out.append([day, EXPENSE_LINE, "", code_, name, line.contact_name, words,
                        _money(line.amount or 0), "", "", "", "", "", "", "", "", "", code])
    return out


def to_csv(body) -> bytes:
    """The file: this report's header, then ``body``; UTF-8 without a byte-order mark, LF."""
    return _to_csv(body, columns=COLUMNS)
