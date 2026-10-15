"""Adding a supplier: a new contact in the company's Xero organisation AND its synced row
(``xero_contact_sync``) - Flask's ``blueprints/entity/services/suppliers.py``, ported. The access
token comes from Flask (``core/xero_tokens.py``); Flask retried once after its own refresh on a
401/403, which is the token service's job here, so a refusal from Xero is final.
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.db import transaction

from core.xero_tokens import XeroTokenUnavailable, xero_token
from shared_models.models import Entity, XeroContactSync

logger = logging.getLogger("petty-cash-api")

NOT_CONNECTED = "This entity isn't connected to Xero yet. Could you ask an admin to connect it?"
NO_ENTRY = "Xero wouldn't let me in. Could you ask an admin to reconnect it?"
NOT_ADDED = "I couldn't add that contact to Xero. Mind trying again?"
NO_ID = "Xero didn't tell me which contact it created. Mind trying again?"
NO_TOKEN_SERVICE = "I couldn't reach Xero just now. Mind trying again in a moment?"
#: ``xero_contact_sync.name``
MAX_NAME = 150


class SupplierRefused(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message = message
        self.status = status


def create_supplier(entity_id, name: str) -> XeroContactSync:
    """Create ``name`` as a contact in the company's Xero organisation and store its synced row
    (found again by its Xero id if one exists)."""
    entity = Entity.objects.filter(id=entity_id).first()
    if entity is None or not entity.xero_org_id:
        raise SupplierRefused(NOT_CONNECTED)
    try:
        token = xero_token(entity_id)
    except XeroTokenUnavailable as unavailable:
        if unavailable.reconnect:
            raise SupplierRefused(NO_ENTRY) from None
        raise SupplierRefused(NO_TOKEN_SERVICE, 503) from None

    try:
        response = requests.post(
            f"{settings.XERO_API_BASE_URL}/Contacts",
            headers={"Authorization": f"Bearer {token.access_token}", "Accept": "application/json",
                     "Content-Type": "application/json", "Xero-Tenant-Id": str(entity.xero_org_id)},
            json={"Contacts": [{"Name": name, "IsCustomer": True, "IsSupplier": False}]},
            timeout=30,
        )
    except requests.RequestException as exc:
        logger.error("Xero unreachable adding a contact for company %s: %s", entity_id, exc)
        raise SupplierRefused(NOT_ADDED) from None
    if response.status_code in (401, 403):
        logger.error("Xero refused the token adding a contact for company %s: %s", entity_id, response.status_code)
        raise SupplierRefused(NO_ENTRY)
    if response.status_code not in (200, 201):
        logger.error("Xero refused a new contact for company %s: %s %s", entity_id, response.status_code,
                     response.text[:500])
        raise SupplierRefused(NOT_ADDED)
    try:
        contacts = (response.json() or {}).get("Contacts") or []
    except ValueError:
        contacts = []
    if not contacts:
        raise SupplierRefused(NOT_ADDED)
    xero_contact_id = contacts[0].get("ContactID")
    if not xero_contact_id:
        logger.error("Xero created a contact for company %s without a ContactID", entity_id)
        raise SupplierRefused(NO_ID)
    created_name = (contacts[0].get("Name") or name)[:MAX_NAME]

    with transaction.atomic():
        row = XeroContactSync.objects.filter(entity_id=entity_id, xero_contact_id=xero_contact_id).first()
        if row is None:
            row = XeroContactSync.objects.create(entity_id=entity_id, xero_contact_id=xero_contact_id,
                                                 xero_org_id=entity.xero_org_id, name=created_name)
        else:
            row.name = created_name
            row.save(update_fields=["name"])
    logger.info("Supplier %s added for company %s", xero_contact_id, entity_id)
    return row
