"""A line's receipt on its Xero bank transaction, through Xero's Files API - Flask's ``publish.py``
``upload_each_file`` and helpers (Phase 3 of the Flask retirement, 2026-11-05).

Republishing runs every step again, so the order is: leave the receipt alone when Xero already
holds exactly it (one file, same name and size); otherwise attach the new file and only then
delete the old ones, so a failed upload never leaves the transaction with none.
"""

from __future__ import annotations

import logging
import mimetypes

import requests
from minty_db.models import Attachment, ReportExpenseAttachment

from petty_cash.files import storage
from petty_cash.files.receipt_keys import safe_stem

logger = logging.getLogger("petty-cash-api")

FILES_API = "https://api.xero.com/files.xro/1.0"
#: Generous: the publish may send a 10 MB receipt.
RECEIPT_LIMIT = 32 * 1024 * 1024


def _headers(run, **extra) -> dict[str, str]:
    return {"Authorization": f"Bearer {run.access_token}", "Xero-Tenant-Id": str(run.org),
            "Accept": "application/json", **extra}


def first_receipt_key(line) -> str | None:
    """The line's first receipt (by its order on the line)."""
    link = (ReportExpenseAttachment.objects.filter(report_expense_id=line.id).order_by("sort_order")
            .values_list("attachment_id", flat=True).first())
    if link is None:
        return None
    return Attachment.objects.filter(id=link).values_list("file_path", flat=True).first()


def _associations(run, object_id) -> list[dict] | None:
    """The files on a Xero object; None when unknown (never delete on a guess)."""
    try:
        response = requests.get(f"{FILES_API}/Associations/{object_id}", headers=_headers(run), timeout=(10, 30))
    except requests.RequestException as exc:
        logger.warning("Publish: could not list Xero files on %s: %s", object_id, exc)
        return None
    if response.status_code == 404:
        return []
    if response.status_code != 200:
        logger.warning("Publish: listing Xero files on %s answered %s", object_id, response.status_code)
        return None
    try:
        found = response.json() or []
    except ValueError:
        return None
    return [a for a in found if (a or {}).get("FileId")]


def _already_attached(associations: list[dict], name: str, data: bytes) -> bool:
    if len(associations) != 1 or (associations[0].get("Name") or "") != name:
        return False
    try:
        return int(associations[0].get("Size") or -1) == len(data)
    except (TypeError, ValueError):
        return False


def _upload(run, name: str, data: bytes, content_type: str) -> str | None:
    try:
        # The multipart part name must be the file name, extension and all, or Xero refuses it.
        response = requests.post(f"{FILES_API}/Files", headers=_headers(run),
                                 files={name: (name, data, content_type)}, timeout=(10, 60))
    except requests.RequestException as exc:
        logger.error("Publish: uploading %s to Xero failed: %s", name, exc)
        return None
    if response.status_code not in (200, 201):
        logger.error("Publish: Xero refused file %s: %s %s", name, response.status_code, (response.text or "")[:300])
        return None
    try:
        body = response.json() or {}
    except ValueError:
        logger.error("Publish: Xero's file answer for %s was not JSON", name)
        return None
    file_id = body.get("FileId") or body.get("Id")
    if not file_id:
        logger.error("Publish: Xero gave no FileId for %s", name)
    return file_id or None


def _associate(run, file_id: str, object_id: str) -> bool:
    try:
        response = requests.post(f"{FILES_API}/Files/{file_id}/Associations",
                                 headers=_headers(run, **{"Content-Type": "application/json"}),
                                 json={"ObjectId": str(object_id), "ObjectGroup": "BankTransaction"}, timeout=(10, 30))
    except requests.RequestException as exc:
        logger.error("Publish: attaching file %s to %s failed: %s", file_id, object_id, exc)
        return False
    if response.status_code in (200, 201):
        return True
    logger.error("Publish: Xero refused attaching file %s to %s: %s", file_id, object_id, response.status_code)
    return False


def _delete(run, file_ids: list[str]) -> None:
    for file_id in file_ids:
        try:
            response = requests.delete(f"{FILES_API}/Files/{file_id}", headers=_headers(run), timeout=(10, 30))
            if response.status_code not in (200, 204):
                logger.warning("Publish: could not delete superseded Xero file %s: %s", file_id, response.status_code)
        except requests.RequestException as exc:
            logger.warning("Publish: deleting Xero file %s failed: %s", file_id, exc)


def attach_receipt(run, line, transaction_id: str) -> bool:
    """Put the line's first receipt on its bank transaction. False (logged) when it did not; the
    transaction itself stands either way."""
    key = first_receipt_key(line)
    if not key:
        logger.info("Publish: line %s has no receipt", line.id)
        return False
    name = (safe_stem(line.remarks or line.item) or "RECEIPT") + "." + key.split(".")[-1]
    try:
        data = storage.read_receipt(key, RECEIPT_LIMIT)
    except Exception as exc:  # the bucket's own errors are many; the transaction stands
        logger.error("Publish: could not read receipt %s from the bucket: %s", key, exc)
        return False
    existing = _associations(run, transaction_id)
    if existing is not None and _already_attached(existing, name, data):
        logger.info("Publish: the receipt on %s is unchanged", transaction_id)
        return True
    file_id = _upload(run, name, data, mimetypes.guess_type(name)[0] or "application/octet-stream")
    if not file_id or not _associate(run, file_id, transaction_id):
        return False
    _delete(run, [a["FileId"] for a in (existing or []) if a["FileId"] != file_id])
    logger.info("Publish: receipt attached to %s (file %s)", transaction_id, file_id)
    return True
