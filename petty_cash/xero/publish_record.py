"""Which Xero objects a report's publish created - Flask's ``xero/services/publish_record.py``.

A publish posts to Xero's collection endpoints, so without a record of what came back a republish
would create a second copy of every transaction. The record is JSON in
``xero_report_sync.xero_response_text`` (one row per report)::

    {"version": 1,
     "org": "<entities.xero_org_id at publish time>",
     "objects": {
        "invoices": {"type": "INVOICE", "id": "..."},
        "deposit":  {"type": "BANK_TRANSFER", "id": "...", "amount": 12.0},
        "expenses": {"<report_expense.id>": {"type": "BANK_TRANSACTION", "id": "..."}}}}

Flask's format exactly, because Flask's publish reads and writes the same rows until the release.
Each write is saved at once (never inside the caller's transaction): an object Xero has created
must be remembered even if a later step fails.
"""

from __future__ import annotations

import json
import logging

from django.db import DatabaseError
from django.utils import timezone
from minty_db.models import XeroReportSync

logger = logging.getLogger("petty-cash-api")

RECORD_VERSION = 1


def _empty(org_id=None) -> dict:
    return {"version": RECORD_VERSION, "org": str(org_id or ""), "objects": {}}


def load_record(report_id, current_org_id=None) -> dict:
    """The report's record, or an empty one ("nothing known was published", so callers create).

    A record from another Xero organisation is discarded: its ids mean nothing there. A record
    with no org counts as a match."""
    if not report_id:
        return _empty(current_org_id)
    row = XeroReportSync.objects.filter(report_id=report_id).first()
    if row is None or not row.xero_response_text:
        return _empty(current_org_id)
    try:
        record = json.loads(row.xero_response_text)
    except (ValueError, TypeError):
        logger.warning("xero_report_sync for report %s is not valid JSON; treating the report as unpublished",
                       report_id)
        return _empty(current_org_id)
    if not isinstance(record, dict) or not isinstance(record.get("objects"), dict):
        return _empty(current_org_id)
    recorded_org = str(record.get("org") or "")
    if current_org_id and recorded_org and recorded_org != str(current_org_id):
        logger.info("Report %s was published to Xero org %s but the company is now on %s; ignoring the "
                    "recorded ids", report_id, recorded_org, current_org_id)
        return _empty(current_org_id)
    record.setdefault("version", RECORD_VERSION)
    record.setdefault("org", recorded_org)
    return record


def _save(report_id, record: dict) -> None:
    """Upsert the record onto the report's row. A failed save is logged, never raised: losing the
    row costs a duplicate on the next republish, and must not fail what Xero already accepted."""
    now = timezone.now()
    try:
        row = XeroReportSync.objects.filter(report_id=report_id).first()
        if row is None:
            row = XeroReportSync(report_id=report_id, reported_at=now)
        row.xero_response_text = json.dumps(record)
        row.sync_status = "completed"
        row.completed_at = now
        if row.reported_at is None:
            row.reported_at = now
        row.save()
    except DatabaseError as exc:
        logger.error("Could not record the Xero object ids for report %s: %s", report_id, exc)


def record_object(report_id, org_id, module: str, xero_id, *, source_id=None, object_type=None, **extra) -> None:
    """Remember that ``module`` created ``xero_id`` for this report (an expense under its line id)."""
    if not report_id or not module or not xero_id:
        return
    record = load_record(report_id)
    record["org"] = str(org_id or record.get("org") or "")
    entry = {"type": object_type or "", "id": str(xero_id)}
    entry.update({k: v for k, v in extra.items() if v is not None})
    if source_id:
        bucket = record["objects"].setdefault(module, {})
        if not isinstance(bucket, dict):
            bucket = {}
            record["objects"][module] = bucket
        bucket[str(source_id)] = entry
    else:
        record["objects"][module] = entry
    _save(report_id, record)


def forget_object(report_id, module: str, *, source_id=None) -> None:
    """Drop a recorded object (gone from Xero, or about to be replaced)."""
    if not report_id or not module:
        return
    record = load_record(report_id)
    if module not in record["objects"]:
        return
    if source_id:
        bucket = record["objects"].get(module)
        if isinstance(bucket, dict):
            bucket.pop(str(source_id), None)
            if not bucket:
                record["objects"].pop(module, None)
    else:
        record["objects"].pop(module, None)
    _save(report_id, record)


def recorded_entry(record, module: str, source_id=None) -> dict | None:
    if not isinstance(record, dict):
        return None
    entry = (record.get("objects") or {}).get(module)
    if source_id is not None:
        if not isinstance(entry, dict):
            return None
        entry = entry.get(str(source_id))
    if isinstance(entry, dict) and entry.get("id"):
        return entry
    return None


def recorded_id(record, module: str, source_id=None):
    entry = recorded_entry(record, module, source_id)
    return entry.get("id") if entry else None


def recorded_expense_ids(record) -> dict:
    """``{report_expense id: xero object id}`` for every recorded expense."""
    bucket = (record.get("objects") or {}).get("expenses") if record else None
    if not isinstance(bucket, dict):
        return {}
    return {key: value.get("id") for key, value in bucket.items() if isinstance(value, dict) and value.get("id")}
