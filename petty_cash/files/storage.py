"""Receipts in Backblaze B2 through the S3 API - Flask's ``blueprints/report/services/s3_storage.py``
(``upload_file_to_s3``, ``delete_files_from_s3``), ported.

The key is ``expenses/<report id>/<DD_MON_YYYY>_<ITEM>_<rounded amount>[_n].<ext>``; a name already
in use by another attachment gets a short unique suffix (the second of two lines alike used to
overwrite the first one's receipt). The bytes are stored exactly as uploaded, with no downsizing or
conversion (owner, 2026-11-04), through ``minty_shared.storage.put_file``, which retries twice
(1 s, 2 s). One client per process (``settings.S3``).
"""

from __future__ import annotations

import logging
import os
import uuid
from functools import lru_cache

from botocore.exceptions import ClientError
from django.conf import settings
from minty_db.models import Attachment
from minty_shared.storage import make_client, put_file

from petty_cash.files.receipt_keys import safe_extension, safe_stem

logger = logging.getLogger("petty-cash-api")

#: The magic bytes of the three file types a receipt may be (Flask's ``expense_ai._MAGIC``): the
#: declared extension and the browser's content type are not evidence.
_MAGIC = (
    (b"%PDF", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


def sniff_mime(data: bytes) -> str | None:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    return None


@lru_cache(maxsize=1)
def client():
    s3 = settings.S3
    return make_client(key=s3["key"], secret=s3["secret"], region=s3["region"], endpoint_url=s3["endpoint_url"])


def bucket() -> str:
    name = settings.S3["bucket"]
    if not name:
        raise RuntimeError("S3_URL names no bucket: receipts cannot be stored or read.")
    return name


def receipt_key(report, filename: str, description: str, amount, *, file_index=None) -> str:
    ext = "." + safe_extension(os.path.basename(filename or ""))
    day = report.transaction_date.strftime("%d %b %Y").upper().replace(" ", "_")
    stem = f"{day}_{safe_stem(description) or 'RECEIPT'}_{int(round(float(amount)))}"
    if file_index is not None and file_index > 0:
        stem = f"{stem}_{file_index + 1}"
    return f"expenses/{report.id}/{stem}{ext}"


def upload_receipt(data: bytes, mime: str, report, *, filename: str, description: str, amount,
                   file_index=None) -> tuple[str, str, int]:
    """Store a receipt's bytes as uploaded and return ``(key, mime type, size)``."""
    key = receipt_key(report, filename, description, amount, file_index=file_index)
    if Attachment.objects.filter(file_path=key).exists():
        stem, extension = os.path.splitext(key)
        key = f"{stem}_{uuid.uuid4().hex[:6].upper()}{extension}"
    return key, mime, put_file(client(), bucket(), key, data, content_type=mime or "application/octet-stream")


def delete_files(keys) -> None:
    """Delete stored receipts. A refusal from the bucket is logged (the rows are already gone, as in
    Flask) - never silently: the log names what was left behind."""
    keys = [k for k in keys if k]
    if not keys:
        return
    try:
        client().delete_objects(Bucket=bucket(), Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True})
    except ClientError as exc:
        logger.error("Receipts left in the bucket after their lines were deleted %s: %s", keys, exc)


def read_receipt(key: str, limit: int) -> bytes:
    """At most ``limit`` + 1 bytes of a stored receipt (the caller refuses anything over ``limit``)."""
    obj = client().get_object(Bucket=bucket(), Key=key)
    return obj["Body"].read(limit + 1)
