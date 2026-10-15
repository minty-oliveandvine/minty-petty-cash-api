"""Shared test fixtures: people, companies and the module token Flask would mint.

The rows are written through the models, so the same fixtures run on SQLite (tables built from
the models) and on the Postgres harness (tables from Minty's schema file).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

import jwt
import pytest
from django.conf import settings
from django.test import Client

from shared_models.enums import CashType, EntityRole, EntityStatus, ModuleCode, SystemRole
from shared_models.models import (
    CashInfo,
    CountryInfo,
    CurrencyInfo,
    Entity,
    EntityFunction,
    EntityFunctionMap,
    User,
    UserEntity,
)

WEB = "http://localhost:3010"


def mint(user_id, *, module="billing", secret=None, expires_in=1800, **claims) -> str:
    """A module token as Flask's ``_generate_module_token`` mints it."""
    now = int(time.time())
    payload = {"user_id": str(user_id), "module": module, "iat": now, "exp": now + expires_in, **claims}
    return jwt.encode(payload, secret or settings.SECRET_KEY, algorithm="HS256")


def headers(user_id, **kwargs) -> dict:
    return {"Authorization": f"Bearer {mint(user_id, **kwargs)}", "Origin": WEB}


@dataclass
class Shop:
    owner: User
    entity: Entity
    currency: CurrencyInfo


def make_user(email="owner@test.com", *, first_name="Olive", last_name="Owner", approved=True,
              system_role=SystemRole.NORMAL) -> User:
    return User.objects.create(
        id=str(uuid.uuid4()), email=email, username=email, password="x", first_name=first_name,
        last_name=last_name, approved=approved, system_role=system_role,
    )


def make_currency(code="HKD") -> CurrencyInfo:
    found = CurrencyInfo.objects.filter(currency_code=code).first()
    return found or CurrencyInfo.objects.create(
        id=str(uuid.uuid4()), currency_code=code, currency_name=code, symbol="$", decimal_places=2
    )


def petty_cash_module() -> EntityFunction:
    found = EntityFunction.objects.filter(function_code=ModuleCode.PETTY_CASH).first()
    return found or EntityFunction.objects.create(
        id=str(uuid.uuid4()), function_code=ModuleCode.PETTY_CASH, function_name="Petty Cash"
    )


def make_entity(owner, *, name="Acme Shop", role=EntityRole.ADMIN, status=EntityStatus.DISCONNECTED,
                petty_cash=True, currency=None) -> Entity:
    currency = currency or make_currency()
    country = CountryInfo.objects.filter(country_code="HK").first() or CountryInfo.objects.create(
        country_code="HK", alpha3_code="HKG", country_name_en="Hong Kong", currency_id=currency.id
    )
    entity = Entity.objects.create(
        id=str(uuid.uuid4()), name=name, status=status, currency_id=currency.id,
        country_code=country.country_code,
    )
    UserEntity.objects.create(user_id=owner.id, entity_id=entity.id, role=role, approved=True)
    EntityFunctionMap.objects.create(
        entity_id=entity.id, entity_function_id=petty_cash_module().id, is_enabled=petty_cash,
        created_by=owner.id,
    )
    return entity


@pytest.fixture
def shop(db) -> Shop:
    owner = make_user()
    currency = make_currency()
    return Shop(owner=owner, entity=make_entity(owner, currency=currency), currency=currency)


@pytest.fixture
def client() -> Client:
    return Client()


def url(entity_id, rest="") -> str:
    return f"/api/petty-cash/v1/companies/{entity_id}{rest}"


def make_report(entity, day, *, status="submitted", created_by=None, **fields):
    """A report row as Flask's ``Report(...)`` inserts it - its Python defaults spelled out
    (rule 0: SQLAlchemy's ``default=`` never reaches a Django insert)."""
    from shared_models.models import Report

    values = {"cash_addition": 0, "cashsale_total": 0, "nocashsale_total": 0, "total_sales": 0,
              "bank_deposit": 0, "discrepancy_amount": 0, "xero_integrated": False}
    values.update(fields)
    return Report.objects.create(
        id=str(uuid.uuid4()), entity_id=entity.id, transaction_date=day, status=status,
        created_by=created_by, **values,
    )


#: Flask's ``seed_currency`` faces: HKD 1000 .. 0.1.
FACES = (1000, 500, 100, 50, 20, 10, 5, 2, 1, 0.5, 0.2, 0.1)


@pytest.fixture
def notes(shop) -> dict:
    """The shop's currency's notes and coins as Flask's ``seed_currency`` makes them (notes from
    10 up). face value -> denomination id."""
    ids = {}
    for order, value in enumerate(FACES, start=1):
        row = CashInfo.objects.create(currency_id=shop.currency.id, cash_value=Decimal(str(value)),
                                      type=CashType.NOTE if value >= 10 else CashType.COIN, cash_name=str(value),
                                      display_order=order, is_active=True)
        ids[value] = str(row.id)
    return ids
