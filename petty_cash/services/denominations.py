"""The company's notes and coins - Flask's ``blueprints/report/services/cash_denominations.py``
(``resolve_denominations_for_entity``, ``build_denomination_rows``, ``save_cash_count_details``,
``refresh_cash_stock``), ported.

The catalogue is the active ``cash_info`` rows of the company's currency (``entities.currency_id``,
else its country's), ordered by ``display_order`` then value high to low; the company's
``entity_cash_setting`` rows switch a denomination off or reorder it. A denomination with no setting
is on. The company's order is applied to a COPY - Flask set it onto the shared catalogue rows in
memory, where a later commit could write it back for every company.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import F, Sum

from petty_cash.money import cents
from shared_models.enums import ReportStatus
from shared_models.models import (
    CashInfo,
    CountryInfo,
    Entity,
    EntityCashDetail,
    EntityCashSetting,
    Report,
    ReportCashCount,
)

logger = logging.getLogger("petty-cash-api")


@dataclass(frozen=True)
class Denomination:
    cash_id: str
    value: Decimal
    kind: str | None
    name: str | None
    display_order: int


def currency_id_for(entity: Entity | None) -> str | None:
    if entity is None:
        return None
    if entity.currency_id:
        return entity.currency_id
    if entity.country_code:
        return CountryInfo.objects.filter(country_code=entity.country_code).values_list("currency_id", flat=True).first()
    return None


def denominations_for(entity_id) -> list[Denomination]:
    """The denominations the company counts, in its order. Empty when its currency has none."""
    currency_id = currency_id_for(Entity.objects.filter(id=entity_id).first())
    if not currency_id:
        logger.warning("Company %s has no resolvable currency - no denominations", entity_id)
        return []
    catalogue = list(
        CashInfo.objects.filter(currency_id=currency_id, is_active=True).order_by("display_order", "-cash_value")
    )
    if not catalogue:
        logger.warning("No active denominations for currency %s (company %s)", currency_id, entity_id)
        return []
    overrides = {row.cash_id: row for row in EntityCashSetting.objects.filter(entity_id=entity_id)}
    resolved = []
    for cash in catalogue:
        override = overrides.get(cash.id)
        if override is not None and not override.is_active:
            continue
        order = override.display_order if override is not None and override.display_order is not None else cash.display_order
        resolved.append(Denomination(cash_id=cash.id, value=cents(cash.cash_value),
                                     kind=str(cash.type) if cash.type else None, name=cash.cash_name,
                                     display_order=order))
    resolved.sort(key=lambda d: (d.display_order, -d.value))
    return resolved


def denomination_rows(entity_id, report_id) -> list[dict]:
    """The denominations with the report's saved counts (0 without a row)."""
    saved = dict(ReportCashCount.objects.filter(report_id=report_id).values_list("cash_id", "quantity"))
    return [
        {"cash_id": d.cash_id, "value": float(d.value), "type": d.kind, "name": d.name,
         "count": saved.get(d.cash_id, 0)}
        for d in denominations_for(entity_id)
    ]


def save_counts(report_id, counts: dict) -> None:
    """Upsert the counted quantities; a zero removes the row. The face value in force is
    snapshotted on the row. No commit - the caller's transaction holds the count and its
    difference together."""
    existing = {row.cash_id: row for row in ReportCashCount.objects.filter(report_id=report_id)}
    faces = dict(CashInfo.objects.filter(id__in=list(counts)).values_list("id", "cash_value"))
    for cash_id, count in counts.items():
        count = int(count or 0)
        row = existing.get(cash_id)
        if count == 0:
            if row is not None:
                row.delete()
            continue
        if row is None:
            ReportCashCount.objects.create(report_id=report_id, cash_id=cash_id, quantity=count,
                                           cash_value=faces.get(cash_id, Decimal("0")))
        else:
            row.quantity = count
            row.save(update_fields=["quantity"])


def refresh_cash_stock(entity_id) -> None:
    """Rebuild the company's ``entity_cash_detail`` from its latest SUBMITTED report: one row per
    active denomination of its currency (other currencies' rows deleted), ``cash_instock`` = the
    money that report counted for it (0 when not counted). No commit."""
    currency_id = currency_id_for(Entity.objects.filter(id=entity_id).first())
    catalogue = list(CashInfo.objects.filter(currency_id=currency_id, is_active=True)) if currency_id else []
    if not catalogue:
        logger.warning("Company %s has no active denominations for its currency - its stock rows are cleared",
                       entity_id)
    wanted = {cash.id: cash for cash in catalogue}
    EntityCashDetail.objects.filter(entity_id=entity_id).exclude(cash_id__in=list(wanted)).delete()
    latest = (
        Report.objects.filter(entity_id=entity_id).exclude(status=ReportStatus.DRAFT).order_by("-transaction_date").first()
    )
    counted = {}
    if latest is not None:
        counted = {
            row["cash_id"]: cents(row["money"])
            for row in ReportCashCount.objects.filter(report_id=latest.id)
            .values("cash_id")
            .annotate(money=Sum(F("quantity") * F("cash_value")))
        }
    present = set(EntityCashDetail.objects.filter(entity_id=entity_id).values_list("cash_id", flat=True))
    for cash_id, cash in wanted.items():
        values = {"cash_type": cash.type, "cash_instock": counted.get(cash_id, Decimal("0.00"))}
        if cash_id in present:
            EntityCashDetail.objects.filter(entity_id=entity_id, cash_id=cash_id).update(**values)
        else:
            EntityCashDetail.objects.create(entity_id=entity_id, cash_id=cash_id, **values)
