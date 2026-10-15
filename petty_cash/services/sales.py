"""The report wizard's Sales step - Flask's ``blueprints/report/services/sales.py`` (and the totals
helpers it calls in ``shared.py``), ported.

Flask's rules: a save needs the day's draft; every method the page shows gets a ``report_sale``
row, a method left out a zero; methods the page has no field for (an ``other`` one that is not
Cash) are left alone; the closing balance is recomputed AFTER the rows are written; the saver
becomes the report's creator ("track last editor").
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from petty_cash.money import cents
from petty_cash.services.reports import log_history, update_draft_progress
from shared_models.enums import SaleType
from shared_models.models import EntitySaleSetting, Report, ReportSale, SaleInfo

#: A method the page lists but the save has always skipped (the old Deliveroo column).
SKIPPED = "deliveroo_sales"


def sales_methods(entity_id) -> list[dict]:
    """The company's enabled methods in Settings order (``display_order``, then name), each with
    its catalogue row's ``sale_name`` / ``value_name`` / ``type``."""
    links = list(
        EntitySaleSetting.objects.filter(entity_id=entity_id, is_active=True).values("sale_id", "display_order")
    )
    catalogue = {
        row["id"]: row
        for row in SaleInfo.objects.filter(id__in=[link["sale_id"] for link in links]).values(
            "id", "sale_name", "value_name", "type"
        )
    }
    methods = [
        {**catalogue[link["sale_id"]], "sale_id": link["sale_id"], "display_order": link["display_order"]}
        for link in links
        if link["sale_id"] in catalogue
    ]
    # Postgres sorts a NULL display_order last in ascending order (Flask's query relies on it).
    methods.sort(key=lambda m: (m["display_order"] is None, m["display_order"] or 0, m["sale_name"]))
    return methods


def shown(method: dict) -> bool:
    """The page has a field for the electronic and delivery methods, and for Cash."""
    return method["type"] in (SaleType.ELECTRONIC, SaleType.DELIVERY) or method["value_name"] == SaleInfo.CASH_VALUE_NAME


def page_methods(entity_id, report_id=None) -> list[dict]:
    """The methods the page shows, in order, each with the report's amount (None without a row)."""
    amounts = {}
    if report_id is not None:
        amounts = dict(ReportSale.objects.filter(report_id=report_id).values_list("sale_id", "amount"))
    return [
        {
            "sale_id": str(m["sale_id"]),
            "name": m["sale_name"],
            "value_name": m["value_name"],
            "type": str(m["type"]),
            "amount": amounts.get(m["sale_id"]),
        }
        for m in sales_methods(entity_id)
        if m["value_name"] and shown(m)
    ]


def cash_sales_from_detail(report: Report) -> Decimal:
    """The Cash method's row, else the report's cash column (Flask's ``get_cash_sales_from_detail``)."""
    cash_id = SaleInfo.objects.filter(value_name=SaleInfo.CASH_VALUE_NAME).values_list("id", flat=True).first()
    total = sum(
        (cents(a) for a in ReportSale.objects.filter(report_id=report.id, sale_id=cash_id).values_list("amount", flat=True)),
        Decimal("0.00"),
    ) if cash_id else Decimal("0.00")
    return total if total > 0 else cents(report.cashsale_total)


def refresh_sales_totals(report: Report) -> None:
    """``total_sales`` = every method's rows (the column's cash too when no Cash row holds it),
    ``nocashsale_total`` = that less the cash (Flask's ``update_report_draft_sales_from_detail``)."""
    by_sale = {}
    for sale_id, amount in ReportSale.objects.filter(report_id=report.id).values_list("sale_id", "amount"):
        by_sale[sale_id] = by_sale.get(sale_id, Decimal("0.00")) + cents(amount)
    names = dict(SaleInfo.objects.filter(id__in=list(by_sale)).values_list("id", "value_name"))
    # Flask's ``if value_name and amount``: a catalogue row with no value_name, or a zero, adds nothing.
    counted = {sid: v for sid, v in by_sale.items() if names.get(sid) and v}
    cash = cents(report.cashsale_total)
    total = sum(counted.values(), Decimal("0.00"))
    cash_in_rows = any(names[sid] == SaleInfo.CASH_VALUE_NAME for sid in counted)
    if cash > 0 and not cash_in_rows:
        total += cash
    report.total_sales = total
    report.nocashsale_total = total - cash


@transaction.atomic
def save_sales(entity_id, user, draft: Report, amounts: dict, action_type: str) -> Report:
    """Write the Sales step onto ``draft``: one ``report_sale`` row per method the page shows
    (``amounts`` maps value_name -> amount; a missing method is zero), the totals, the closing
    balance, the progress and a history line - one transaction."""
    draft = Report.objects.select_for_update().get(id=draft.id)
    draft.created_by = user.id
    cash = cents(amounts.get(SaleInfo.CASH_VALUE_NAME) or 0)
    draft.cashsale_total = cash

    shop_total, delivery_total = cash, Decimal("0.00")
    for method in sales_methods(entity_id):
        field = method["value_name"]
        if not field or field == SKIPPED or not shown(method):
            continue
        amount = cents(amounts.get(field) or 0)
        if field != SaleInfo.CASH_VALUE_NAME:
            if method["type"] == SaleType.DELIVERY:
                delivery_total += amount
            else:
                shop_total += amount
        updated = ReportSale.objects.filter(report_id=draft.id, sale_id=method["sale_id"]).update(amount=amount)
        if not updated:
            ReportSale.objects.create(report_id=draft.id, sale_id=method["sale_id"], amount=amount)

    refresh_sales_totals(draft)
    draft.closing_balance = (
        cents(draft.opening_balance)
        + cents(draft.cash_addition)
        + cash_sales_from_detail(draft)
        - cents(draft.expense_total)
        - cents(draft.bank_deposit)
    )
    update_draft_progress(draft, action_type, "sales", "expenses")
    draft.save()
    log_history(
        draft,
        user,
        action="updated",
        field_changed="sales_entry",
        old_value="previous_sales_state",
        new_value=(
            f"Sales: Shop={float(shop_total)}, Delivery={float(delivery_total)}, "
            f"Total={float(shop_total + delivery_total)}, Closing Balance={float(draft.closing_balance)}"
        ),
    )
    return draft
