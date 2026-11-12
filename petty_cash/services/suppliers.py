"""Adding a supplier: a new contact in the company's Xero organisation AND its synced row
(``xero_contact_sync``) - Flask's ``blueprints/entity/services/suppliers.py``, ported. The access
token comes from Flask (``core/xero_tokens.py``); Flask retried once after its own refresh on a
401/403, which is the token service's job here, so a refusal from Xero is final.

Never blocked behind Xero, never a duplicate (owner, 2026-11-12): a name the company already has
(case and inner spaces ignored) is that supplier; with no live connection the supplier is the
company's own row (no Xero id - the next live sync puts it in Xero); live, Xero's contact of that
name is linked from the stored copies before one is made. A supplier the company deleted is that
one brought back (onboarding-api's chart rule): its archived row active again, or Xero's archived
contact of the name linked again - ACTIVE in Xero when it is there; offline, one still linked to
Xero waits for a reconnect.
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from minty_db.models import Entity, XeroContact, XeroContactSync

from core.xero_tokens import XeroTokenUnavailable, xero_token
from petty_cash.services.xero_settings import minty_on_its_own

logger = logging.getLogger("petty-cash-api")

NOT_CONNECTED = "This entity isn't connected to Xero yet. Could you ask an admin to connect it?"
NO_ENTRY = "Xero wouldn't let me in. Could you ask an admin to reconnect it?"
NOT_ADDED = "I couldn't add that contact to Xero. Mind trying again?"
NO_ID = "Xero didn't tell me which contact it created. Mind trying again?"
NO_TOKEN_SERVICE = "I couldn't reach Xero just now. Mind trying again in a moment?"
NOT_BACK = "I couldn't bring that contact back in Xero. Mind trying again?"
WAITS_FOR_XERO = "{name} was archived in Xero. Could you ask an admin to reconnect Xero to bring it back?"
#: ``xero_contact_sync.name``
MAX_NAME = 150


class SupplierRefused(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message = message
        self.status = status


def _key(name) -> str:
    return " ".join(str(name or "").split()).lower()


def _held(entity_id, name: str) -> XeroContactSync | None:
    """The company's active supplier of that name (case and inner spaces ignored), if any."""
    return next((c for c in XeroContactSync.objects.filter(entity_id=entity_id, record_status="active",
                                                           merged_into__isnull=True)
                 if _key(c.name) == _key(name)), None)


def _kept_in_minty(entity_id, name: str) -> XeroContactSync:
    """No live connection (owner, 2026-11-12; was: only a company keeping its books in Minty): the
    supplier is the company's own row, no Xero id."""
    row = XeroContactSync.objects.create(entity_id=entity_id, name=" ".join(name.split())[:MAX_NAME],
                                         source="user")
    logger.info("Supplier %s kept in Minty for company %s", row.id, entity_id)
    return row


def _access_token(entity_id) -> str:
    try:
        return xero_token(entity_id).access_token
    except XeroTokenUnavailable as unavailable:
        if unavailable.reconnect:
            raise SupplierRefused(NO_ENTRY) from None
        raise SupplierRefused(NO_TOKEN_SERVICE, 503) from None


def _active_in_xero(entity: Entity, guid: str) -> None:
    """Set an archived Xero contact ACTIVE again, and Minty's copy of it with it."""
    token = _access_token(entity.id)
    try:
        response = requests.post(
            f"{settings.XERO_API_BASE_URL}/Contacts",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                     "Content-Type": "application/json", "Xero-Tenant-Id": str(entity.xero_org_id)},
            json={"Contacts": [{"ContactID": guid, "ContactStatus": "ACTIVE"}]},
            timeout=30,
        )
    except requests.RequestException as exc:
        logger.error("Xero unreachable bringing contact %s back for company %s: %s", guid, entity.id, exc)
        raise SupplierRefused(NOT_BACK) from None
    if response.status_code in (401, 403):
        raise SupplierRefused(NO_ENTRY)
    if response.status_code not in (200, 201):
        logger.error("Xero refused bringing contact %s back for company %s: %s %s", guid, entity.id,
                     response.status_code, response.text[:500])
        raise SupplierRefused(NOT_BACK)
    XeroContact.objects.filter(entity_id=entity.id, xero_org_id=str(entity.xero_org_id), xero_guid=guid).update(
        status="ACTIVE")


def _brought_back(entity: Entity, name: str, offline: bool) -> XeroContactSync | None:
    """The supplier the company deleted with this name, brought back; None when there is none."""
    gone = sorted((c for c in XeroContactSync.objects.filter(entity_id=entity.id, record_status="archived",
                                                             merged_into__isnull=True) if _key(c.name) == _key(name)),
                  key=lambda c: not c.xero_contact_id)
    if gone:
        row = gone[0]
        if row.xero_contact_id:
            if offline:
                raise SupplierRefused(WAITS_FOR_XERO.format(name=row.name))
            _active_in_xero(entity, row.xero_contact_id)
        XeroContactSync.objects.filter(id=row.id).update(record_status="active")
        row.record_status = "active"
        logger.info("Supplier %s brought back for company %s", row.id, entity.id)
        return row
    if offline:
        return None
    held = set(XeroContactSync.objects.filter(entity_id=entity.id, xero_contact_id__isnull=False)
               .values_list("xero_contact_id", flat=True))
    copy = next((c for c in XeroContact.objects.filter(entity_id=entity.id, xero_org_id=str(entity.xero_org_id))
                 .exclude(status="ACTIVE") if _key(c.name) == _key(name) and c.xero_guid not in held), None)
    if copy is None:
        return None
    _active_in_xero(entity, copy.xero_guid)
    row = XeroContactSync.objects.create(entity_id=entity.id, xero_contact_id=copy.xero_guid,
                                         xero_org_id=entity.xero_org_id, name=copy.name[:MAX_NAME],
                                         xero_contact_link_id=copy.id)
    logger.info("Supplier %s brought back from Xero's archive for company %s", row.id, entity.id)
    return row


def create_supplier(entity_id, name: str) -> XeroContactSync:
    """The company's supplier of ``name``: the one it has, else - live on Xero - Xero's of that
    name or a new one made there, else its own Minty row."""
    entity = Entity.objects.filter(id=entity_id).first()
    if entity is None:
        raise SupplierRefused(NOT_CONNECTED)
    held = _held(entity_id, name)
    if held is not None:
        return held
    # offline: no organisation; disconnected (a disconnect keeps the organisation, 2026-11-10); or
    # still keeping its books in Minty - mid-matching too, whose Finish links its suppliers
    offline = not entity.xero_org_id or entity.status == "disconnected" or minty_on_its_own(entity_id)
    back = _brought_back(entity, name, offline)
    if back is not None:
        return back
    if offline:
        return _kept_in_minty(entity_id, name)
    copy = next((c for c in XeroContact.objects.filter(entity_id=entity_id, xero_org_id=str(entity.xero_org_id),
                                                       status="ACTIVE")
                 if _key(c.name) == _key(name)
                 and not XeroContactSync.objects.filter(entity_id=entity_id, xero_contact_id=c.xero_guid).exists()),
                None)
    if copy is not None:
        row = XeroContactSync.objects.create(entity_id=entity_id, xero_contact_id=copy.xero_guid,
                                             xero_org_id=entity.xero_org_id, name=" ".join(name.split())[:MAX_NAME],
                                             xero_contact_link_id=copy.id)
        logger.info("Supplier %s linked to Xero's contact of its name for company %s", row.id, entity_id)
        return row
    token = _access_token(entity_id)
    try:
        response = requests.post(
            f"{settings.XERO_API_BASE_URL}/Contacts",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
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
            try:
                with transaction.atomic():
                    row = XeroContactSync.objects.create(entity_id=entity_id, xero_contact_id=xero_contact_id,
                                                         xero_org_id=entity.xero_org_id, name=created_name)
            except IntegrityError:
                # Flask's sync stored the same contact since the look-up (uq_xcs_entity_contact,
                # 2026-11-03): that row is this supplier.
                row = XeroContactSync.objects.get(entity_id=entity_id, xero_contact_id=xero_contact_id)
        if row.name != created_name:
            row.name = created_name
            row.save(update_fields=["name"])
    logger.info("Supplier %s added for company %s", xero_contact_id, entity_id)
    return row
