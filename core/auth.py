"""The door every Petty Cash API request goes through - Flask's ``petty_cash_api/guard.py``, in its
order, and it FAILS CLOSED:

1. a valid MODULE token - HS256 with the shared ``SECRET_KEY``, the ``module`` claim Flask's
   ``_generate_module_token`` stamps (``billing``; an onboarding token is the onboarding API's
   alone) and a ``user_id`` - else 401 ``unauthorized``;
2. an approved person who still exists - else 401;
3. a company they belong to (an approved membership), or any company for a system superuser -
   else 403;
4. a company past onboarding - else 409;
5. Petty Cash switched on for it - else 403.

THE COMPANY IS THE ADDRESS'S, never the token's ``entity_id`` claim nor a header: it is checked
against membership on every request, so a token minted for one company opens no other. The
preflight never reaches here (django-cors-headers answers it).

A refusal after authentication is raised as ``Refused`` so it keeps its status and sentence
(``core/exceptions.py``); ninja turns a ``None`` from ``authenticate`` into the 401.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import jwt
from django.conf import settings
from minty_shared import logctx, tokens
from ninja.security import HttpBearer

from core.entitlements import is_module_enabled
from core.exceptions import Refused
from core.policy import has_entity_access, is_superuser
from shared_models.enums import EntityStatus, ModuleCode, entity_role_label
from shared_models.models import Entity, User, UserEntity

logger = logging.getLogger("petty-cash-api")

#: The ``module`` claim on a module token (Flask's ``entity/routes/modules.py::MODULE_TOKEN_CLAIM``).
MODULE_TOKEN_CLAIM = "billing"

NO_COMPANY = "You don't have access to that company."
ONBOARDING = "This company is still setting up. Finish setting it up to use Petty Cash."
NO_MODULE = (
    "Petty Cash isn't switched on for this company. An admin can turn it on in the company's modules."
)


@dataclass(frozen=True)
class Company:
    """The company the door let the person into - Flask's ``profile.entity_context`` dict."""

    id: str
    name: str
    role: str | None
    role_label: str | None
    currency_id: str | None
    country_code: str | None


def _uuid(value) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        return None


def company_for(user: User, entity_id) -> Company | None:
    """The company as the person may see it, or None - ``profile.entity_context``'s rule: a real
    company, and the person a superuser or an approved member."""
    entity_id = _uuid(entity_id)
    if entity_id is None:
        return None  # not an id at all: Postgres would refuse the comparison outright
    org = Entity.objects.filter(id=entity_id).first()
    if org is None or not (is_superuser(user) or has_entity_access(user, entity_id)):
        return None
    role = (
        UserEntity.objects.filter(user_id=user.id, entity_id=entity_id).values_list("role", flat=True).first()
    )
    role = str(role) if role else None
    return Company(
        id=str(org.id),
        name=org.name,
        role=role,
        role_label=entity_role_label(role),
        currency_id=org.currency_id,
        country_code=org.country_code,
    )


class PettyCashBearerAuth(HttpBearer):
    def authenticate(self, request, token):
        try:
            # HS256 with the clock skew every Minty backend allows (minty_shared.tokens).
            claims = tokens.decode_bearer(token, settings.SECRET_KEY)
        except jwt.PyJWTError as exc:
            logger.info("Petty Cash API: token refused (%s)", type(exc).__name__)
            return None
        user_id = _uuid(claims.get("user_id"))
        if claims.get("module") != MODULE_TOKEN_CLAIM or user_id is None:
            return None
        user = User.objects.filter(id=user_id).first()
        if user is None or not user.approved:
            return None
        # The person is known from here on (a signed, unexpired token for an approved user), so
        # the refusals below name them; the company only once every check has passed.
        logctx.bind(user=user.id)

        entity_id = (request.resolver_match.kwargs if request.resolver_match else {}).get("entity_id")
        company = company_for(user, entity_id)
        if company is None:
            raise Refused(NO_COMPANY, 403)
        org_status = Entity.objects.filter(id=company.id).values_list("status", flat=True).first()
        if org_status == EntityStatus.ONBOARDING:
            raise Refused(ONBOARDING, 409)
        if not is_module_enabled(company.id, ModuleCode.PETTY_CASH):
            raise Refused(NO_MODULE, 403)

        request.auth_user = user
        request.company = company
        logctx.bind(entity=company.id)
        return user
