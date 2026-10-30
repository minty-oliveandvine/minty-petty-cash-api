"""Petty Cash settings without Xero - Country & currency and the Electronic / Delivery sales methods -
ported from Flask's ``entity/services/country_currency.py``, ``xero_account_mapping_post.
apply_country_currency_selection``, ``entity/services/cash_settings.sync_cash_settings`` and
``entity/services/payment_methods.py``.

A company's methods are links (``entity_sale_setting``: on/off, order) into one catalogue
(``sale_info``) every company shares: a method nobody has yet adds a catalogue row for everyone, and
a rename of a row another company uses is refused. Cash is a method too, but it is managed on the
report, never here.
"""

from __future__ import annotations

import logging
import uuid

from django.db import transaction
from django.db.models import F, Max, Q
from django.db.models.functions import Lower
from minty_db.enums import SaleType
from minty_db.models import (
    CashInfo,
    CountryInfo,
    CurrencyInfo,
    Entity,
    EntityCashSetting,
    EntitySaleSetting,
    SaleInfo,
)

logger = logging.getLogger("petty-cash-api")

TYPES = ("electronic", "delivery")


class SettingsRefused(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


# --- Country & currency ------------------------------------------------------------------------------


def country_currency_choices(org: Entity) -> dict:
    """The lists the card offers - active rows, plus the company's own even if since deactivated
    (a blank field would read as "unset") - and the current selection in each."""
    countries = list(
        CountryInfo.objects.filter(Q(is_active=True) | Q(country_code=org.country_code))
        .order_by("display_order", "country_name_en").values_list("country_code", "country_name_en")
    )
    currencies = list(
        CurrencyInfo.objects.filter(Q(is_active=True) | Q(id=org.currency_id))
        .order_by("currency_name").values_list("id", "currency_name")
    )
    return {
        "country_code": org.country_code,
        "currency_id": str(org.currency_id) if org.currency_id else None,
        "countries": [{"code": code, "name": name} for code, name in countries],
        "currencies": [{"id": str(cid), "name": name} for cid, name in currencies],
        "selected_country": next((name for code, name in countries if code == org.country_code), None),
        "selected_currency": next((name for cid, name in currencies if str(cid) == str(org.currency_id)), None),
    }


def checked_country_currency(data: dict) -> dict:
    """The ``country_code`` / ``currency_id`` a save sent, checked against the registries - an
    unknown one is refused (422), never skipped."""
    fields = {}
    country_code = str(data.get("country_code") or "").strip().upper()
    if country_code:
        if not CountryInfo.objects.filter(country_code=country_code).exists():
            raise SettingsRefused("Pick one of the countries offered.", 422)
        fields["country_code"] = country_code
    currency_id = str(data.get("currency_id") or "").strip()
    if currency_id:
        try:
            uuid.UUID(currency_id)
        except ValueError:
            raise SettingsRefused("Pick one of the currencies offered.", 422) from None
        if not CurrencyInfo.objects.filter(id=currency_id).exists():
            raise SettingsRefused("Pick one of the currencies offered.", 422)
        fields["currency_id"] = currency_id
    return fields


def sync_cash_settings(entity_id, currency_id) -> None:
    """``entity_cash_setting`` holds the currency's active denominations: new ones added (on, in the
    catalogue's order), others deleted, the company's own choices on the ones kept untouched."""
    wanted = dict(CashInfo.objects.filter(currency_id=currency_id, is_active=True).values_list("id", "display_order"))
    existing = set(EntityCashSetting.objects.filter(entity_id=entity_id).values_list("cash_id", flat=True))
    EntityCashSetting.objects.filter(entity_id=entity_id).exclude(cash_id__in=list(wanted)).delete()
    EntityCashSetting.objects.bulk_create([
        EntityCashSetting(entity_id=entity_id, cash_id=cash_id, is_active=True, display_order=order)
        for cash_id, order in wanted.items() if cash_id not in existing
    ])
    if not wanted:
        logger.warning("cash settings: currency %s has no active denominations (company %s)", currency_id, entity_id)


@transaction.atomic
def save_country_currency(entity_id, fields: dict) -> None:
    """Flask's ``apply_country_currency_selection``: the country, then the currency chosen - or, with
    none chosen, the country's own - and the denominations that follow it. One transaction."""
    org = Entity.objects.select_for_update().get(id=entity_id)
    country = CountryInfo.objects.filter(country_code=fields["country_code"]).first() if "country_code" in fields else None
    if country is not None:
        org.country_code = country.country_code
    currency = CurrencyInfo.objects.filter(id=fields["currency_id"]).first() if "currency_id" in fields else None
    if currency is None and country is not None and country.currency_id:
        currency = CurrencyInfo.objects.filter(id=country.currency_id).first()
    if currency is not None:
        org.currency_id = currency.id
        org.currency_format = currency.symbol or "$"
    org.save(update_fields=["country_code", "currency_id", "currency_format"])
    if currency is not None:
        sync_cash_settings(org.id, currency.id)


# --- Sales methods -----------------------------------------------------------------------------------


def _links(entity_id, *, types=TYPES):
    """The company's switched-on links of ``types`` with their catalogue rows, in list order."""
    links = list(EntitySaleSetting.objects.filter(entity_id=entity_id, is_active=True))
    catalogue = {s.id: s for s in SaleInfo.objects.filter(id__in=[link.sale_id for link in links], type__in=types)}
    rows = [(link, catalogue[link.sale_id]) for link in links if link.sale_id in catalogue]
    # Flask's order: display_order, then the method's name
    rows.sort(key=lambda r: (r[0].display_order is None, r[0].display_order or 0, r[1].sale_name))
    return rows


def methods(entity_id) -> dict:
    """The company's switched-on Electronic / Delivery methods by type, in list order. ``shared``:
    another company has the method too, so a rename would be refused."""
    rows = _links(entity_id)
    shared = set(
        EntitySaleSetting.objects.filter(sale_id__in=[sale.id for _, sale in rows])
        .exclude(entity_id=entity_id).values_list("sale_id", flat=True)
    )
    grouped = {t: [] for t in TYPES}
    for _link, sale in rows:
        grouped[str(sale.type)].append({"id": str(sale.id), "name": sale.sale_name, "value_name": sale.value_name,
                                        "shared": sale.id in shared})
    return grouped


def available(entity_id) -> dict:
    """Catalogue methods the company has NOT switched on, by type - what the add dropdown offers."""
    taken = set(EntitySaleSetting.objects.filter(entity_id=entity_id, is_active=True).values_list("sale_id", flat=True))
    catalogue = (
        SaleInfo.objects.filter(enabled=True, type__in=TYPES)
        .order_by(F("display_order").asc(nulls_last=True), "sale_name")
    )
    grouped = {t: [] for t in TYPES}
    for sale in catalogue:
        if sale.id not in taken:
            grouped[str(sale.type)].append({"id": str(sale.id), "name": sale.sale_name})
    return grouped


def by_name(name: str) -> SaleInfo | None:
    """The catalogue row for a display name, matched case-insensitively."""
    key = (name or "").strip().lower()
    if not key:
        return None
    return SaleInfo.objects.annotate(lname=Lower("sale_name")).filter(lname=key).first()


def value_name_for(name: str) -> str:
    """The form-field name a NEW method gets (``"Tap & Go"`` -> ``tap_&_go_sales``) - Flask's."""
    return (name or "").strip().lower().replace(" ", "_") + "_sales"


@transaction.atomic
def add_method(entity_id, kind: str, *, sale_info_id=None, name: str) -> None:
    """Link a catalogue method to the company, or switch its link back on. The row is the one picked
    (``sale_info_id``), else the one with that name, else a new one - for everyone (Flask's
    ``add_payment_method`` and ``SaleInfo.ensure``)."""
    sale = SaleInfo.objects.filter(id=sale_info_id).first() if sale_info_id else None
    if sale is None:
        sale = by_name(name)
    if sale is None:
        clean = name.strip()
        sale = SaleInfo.objects.create(sale_name=clean, type=SaleType(kind), value_name=value_name_for(clean),
                                       display_order=None, enabled=True)
        logger.info("Sales method catalogue: added %s (%s)", clean, kind)
    link = EntitySaleSetting.objects.select_for_update().filter(entity_id=entity_id, sale_id=sale.id).first()
    if link is not None:
        EntitySaleSetting.objects.filter(entity_id=entity_id, sale_id=sale.id).update(is_active=True)
        return
    last = EntitySaleSetting.objects.filter(entity_id=entity_id).aggregate(m=Max("display_order"))["m"]
    EntitySaleSetting.objects.create(entity_id=entity_id, sale_id=sale.id, is_active=True,
                                     display_order=int(last or 0) + 1)


@transaction.atomic
def rename_method(entity_id, method_id, name: str) -> None:
    """Rename the catalogue row - refused (409) when another company uses it, or another row has
    that name (Flask's ``update_payment_method``)."""
    sale = SaleInfo.objects.select_for_update().get(id=method_id)
    if name == sale.sale_name:
        return
    if EntitySaleSetting.objects.filter(sale_id=sale.id).exclude(entity_id=entity_id).exists():
        raise SettingsRefused("This method is shared with other companies; add a new method instead of renaming it.",
                              409)
    clash = by_name(name)
    if clash is not None and clash.id != sale.id:
        raise SettingsRefused("A method with that name already exists.", 409)
    sale.sale_name = name
    sale.save(update_fields=["sale_name"])


def remove_method(entity_id, method_id) -> None:
    """Switch the method off for the company - the link stays, old reports keep their rows."""
    EntitySaleSetting.objects.filter(entity_id=entity_id, sale_id=method_id).update(is_active=False)


@transaction.atomic
def reorder_methods(entity_id, method_ids: list[str]) -> None:
    for index, method_id in enumerate(method_ids):
        EntitySaleSetting.objects.filter(entity_id=entity_id, sale_id=method_id).update(display_order=index + 1)
