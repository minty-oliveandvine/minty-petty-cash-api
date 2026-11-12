"""Petty Cash settings without Xero - the Electronic / Delivery sales methods - ported from Flask's
``entity/services/payment_methods.py``. The country and currency are not set here: they belong to
minty-web's General tab (Flask's ``PATCH /api/me/company/integration``).

A company's methods are links (``entity_sale_setting``: on/off, order) into ``sale_info``, whose
rows are Minty's catalogue (``entity_id`` NULL: offered to every company, never renamed or deleted -
a company only takes one off its list) or a company's own (``entity_id`` = it: typed by it, its own
to rename or delete; owner, 2026-11-12, minty-db v0.7). A typed name that is Minty's links Minty's
row; anything else becomes the company's own. Cash is a method too, but it is managed on the report,
never here.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import F, Max, Q
from django.db.models.functions import Lower
from minty_db.enums import SaleType
from minty_db.models import EntitySaleSetting, ReportSale, SaleInfo

logger = logging.getLogger("petty-cash-api")

TYPES = ("electronic", "delivery")


class SettingsRefused(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


# --- Sales methods -----------------------------------------------------------------------------------


def _links(entity_id, *, types=TYPES):
    """The company's switched-on links of ``types`` with their catalogue rows, in list order."""
    links = list(EntitySaleSetting.objects.filter(entity_id=entity_id, is_active=True))
    catalogue = {s.id: s for s in SaleInfo.objects.filter(id__in=[link.sale_id for link in links], type__in=types)}
    rows = [(link, catalogue[link.sale_id]) for link in links if link.sale_id in catalogue]
    # Flask's order: display_order, then the method's name
    rows.sort(key=lambda r: (r[0].display_order is None, r[0].display_order or 0, r[1].sale_name))
    return rows


def offered(entity_id) -> Q:
    """The rows a company may use: Minty's catalogue and its own."""
    return Q(entity_id__isnull=True) | Q(entity_id=entity_id)


def methods(entity_id) -> dict:
    """The company's switched-on Electronic / Delivery methods by type, in list order. ``own``: the
    company typed it, so it may rename or delete it; Minty's only come off its list."""
    grouped = {t: [] for t in TYPES}
    for _link, sale in _links(entity_id):
        grouped[str(sale.type)].append({"id": str(sale.id), "name": sale.sale_name, "value_name": sale.value_name,
                                        "own": sale.entity_id is not None})
    return grouped


def available(entity_id) -> dict:
    """Catalogue methods the company has NOT switched on, by type - what the add dropdown offers."""
    taken = set(EntitySaleSetting.objects.filter(entity_id=entity_id, is_active=True).values_list("sale_id", flat=True))
    catalogue = (
        SaleInfo.objects.filter(offered(entity_id), enabled=True, type__in=TYPES)
        .order_by(F("display_order").asc(nulls_last=True), "sale_name")
    )
    grouped = {t: [] for t in TYPES}
    for sale in catalogue:
        if sale.id not in taken:
            grouped[str(sale.type)].append({"id": str(sale.id), "name": sale.sale_name})
    return grouped


def by_name(name: str, entity_id) -> SaleInfo | None:
    """The row a company means by a display name, matched case-insensitively: Minty's first, then
    its own enabled one. Another company's own row is never found."""
    key = (name or "").strip().lower()
    if not key:
        return None
    return (
        SaleInfo.objects.annotate(lname=Lower("sale_name"))
        .filter(Q(entity_id__isnull=True) | Q(entity_id=entity_id, enabled=True), lname=key)
        .order_by(F("entity_id").asc(nulls_first=True))
        .first()
    )


def value_name_for(name: str) -> str:
    """The form-field name a NEW method gets (``"Tap & Go"`` -> ``tap_&_go_sales``) - Flask's."""
    return (name or "").strip().lower().replace(" ", "_") + "_sales"


@transaction.atomic
def add_method(entity_id, kind: str, *, sale_info_id=None, name: str) -> None:
    """Link a method to the company, or switch its link back on. The row is the one picked
    (``sale_info_id``, Minty's or the company's own - the route checks), else Minty's or the
    company's own with that name, else a new row of the company's own (never a new Minty one)."""
    sale = SaleInfo.objects.filter(offered(entity_id), id=sale_info_id).first() if sale_info_id else None
    if sale is None:
        sale = by_name(name, entity_id)
    if sale is None:
        clean = name.strip()
        sale = SaleInfo.objects.create(sale_name=clean, type=SaleType(kind), value_name=value_name_for(clean),
                                       display_order=None, enabled=True, entity_id=entity_id)
        logger.info("Sales method: the company's own %s (%s) added", clean, kind)
    link = EntitySaleSetting.objects.select_for_update().filter(entity_id=entity_id, sale_id=sale.id).first()
    if link is not None:
        EntitySaleSetting.objects.filter(entity_id=entity_id, sale_id=sale.id).update(is_active=True)
        return
    last = EntitySaleSetting.objects.filter(entity_id=entity_id).aggregate(m=Max("display_order"))["m"]
    EntitySaleSetting.objects.create(entity_id=entity_id, sale_id=sale.id, is_active=True,
                                     display_order=int(last or 0) + 1)


@transaction.atomic
def rename_method(entity_id, method_id, name: str) -> None:
    """Rename the company's own method - refused for Minty's (403), and when Minty or the company
    already has that name (409)."""
    sale = SaleInfo.objects.select_for_update().get(id=method_id)
    if sale.entity_id is None:
        raise SettingsRefused(
            f"{sale.sale_name} comes with Minty, so it can't be renamed. "
            "Take it off your list and add your own method instead.", 403)
    if name == sale.sale_name:
        return
    clash = by_name(name, entity_id)
    if clash is not None and clash.id != sale.id:
        raise SettingsRefused("A method with that name already exists.", 409)
    sale.sale_name = name
    sale.save(update_fields=["sale_name"])


@transaction.atomic
def remove_method(entity_id, method_id) -> str:
    """Minty's method comes off the company's list (the link switches off; Add New Method offers it
    again). The company's own is deleted completely: row and link, or - when a report used it - both
    switched off, so the report keeps its amount and nothing offers it again. Returns the sentence."""
    sale = SaleInfo.objects.select_for_update().get(id=method_id)
    links = EntitySaleSetting.objects.filter(entity_id=entity_id, sale_id=sale.id)
    if sale.entity_id is None:
        links.update(is_active=False)
        return f"{sale.sale_name} is off your list. Add it back any time from Add New Method."
    if ReportSale.objects.filter(sale_id=sale.id).exists():
        links.update(is_active=False)
        SaleInfo.objects.filter(id=sale.id).update(enabled=False)
    else:
        links.delete()
        sale.delete()
    logger.info("Sales method: the company's own %s deleted", sale.sale_name)
    return f"{sale.sale_name} deleted."


@transaction.atomic
def reorder_methods(entity_id, method_ids: list[str]) -> None:
    for index, method_id in enumerate(method_ids):
        EntitySaleSetting.objects.filter(entity_id=entity_id, sale_id=method_id).update(display_order=index + 1)
