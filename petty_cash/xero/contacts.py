"""The contact a published bank transaction names - Flask's ``publish.py`` ``create_xero_contact``
and ``ensure_contact_exists_in_xero`` (Phase 3 of the Flask retirement, 2026-11-05).

A line's supplier may have been removed from Xero since it was synced: the publish then makes it
again under the same name, and moves this company's lines from the old id to the new one.
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.db import DatabaseError
from minty_db.models import ReportExpense, XeroContactSync

logger = logging.getLogger("petty-cash-api")

#: Flask's for both contact calls.
TIMEOUT = 10


def create_contact(run, name: str) -> str | None:
    """Make ``name`` a supplier in the company's Xero and store its synced row; the new ContactID,
    or None (logged)."""
    try:
        response = requests.post(
            f"{settings.XERO_API_BASE_URL}/Contacts",
            headers={"Authorization": f"Bearer {run.access_token}", "Accept": "application/json",
                     "Content-Type": "application/json", "Xero-Tenant-Id": str(run.org)},
            json={"Contacts": [{"Name": name, "IsCustomer": False, "IsSupplier": True}]},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.error("Publish: Xero unreachable making contact %r: %s", name, exc)
        return None
    if response.status_code not in (200, 201):
        logger.error("Publish: Xero refused contact %r: %s %s", name, response.status_code, (response.text or "")[:300])
        return None
    try:
        contacts = (response.json() or {}).get("Contacts") or []
    except ValueError:
        contacts = []
    if not contacts or not contacts[0].get("ContactID"):
        logger.error("Publish: Xero made contact %r but gave no ContactID", name)
        return None
    contact_id = contacts[0]["ContactID"]
    try:
        # The organisation it was made in: an org switch clears contacts by org.
        XeroContactSync.objects.create(entity_id=run.entity.id, xero_contact_id=contact_id,
                                       name=contacts[0].get("Name") or name, category="expense_contact",
                                       xero_org_id=run.org)
    except DatabaseError as exc:
        # The contact exists in Xero; the transaction can still name it.
        logger.error("Publish: contact %s made in Xero but not stored: %s", contact_id, exc)
    logger.info("Publish: contact %s made in Xero for %r", contact_id, name)
    return contact_id


def ensure_contact(run, contact_id: str | None, name: str | None) -> str | None:
    """A ContactID Xero knows: ``contact_id`` when Xero has it, else a new contact named ``name``
    (this company's lines move to it). None when neither works: the transaction goes without one."""
    if not contact_id:
        return create_contact(run, name) if name else None
    try:
        response = requests.get(
            f"{settings.XERO_API_BASE_URL}/Contacts/{contact_id}",
            headers={"Authorization": f"Bearer {run.access_token}", "Xero-Tenant-Id": str(run.org),
                     "Accept": "application/json"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.error("Publish: Xero unreachable checking contact %s: %s", contact_id, exc)
        return create_contact(run, name) if name else None
    if response.status_code == 200:
        return contact_id
    if response.status_code != 404:
        logger.error("Publish: checking contact %s answered %s", contact_id, response.status_code)
        return create_contact(run, name) if name else None

    logger.warning("Publish: contact %s is gone from Xero; making %r again", contact_id, name)
    if not name:
        return None
    new_id = create_contact(run, name)
    if new_id:
        replacement = XeroContactSync.objects.filter(entity_id=run.entity.id, xero_contact_id=new_id).first()
        if replacement is not None:
            moved = ReportExpense.objects.filter(contact__xero_contact_id=contact_id,
                                                 contact__entity_id=run.entity.id).update(contact_id=replacement.id)
            logger.info("Publish: %s line(s) moved from contact %s to %s", moved, contact_id, new_id)
    return new_id
