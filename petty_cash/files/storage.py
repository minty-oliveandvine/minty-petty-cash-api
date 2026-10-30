"""Receipts in Backblaze B2 through the S3 API - Flask's ``blueprints/report/services/s3_storage.py``
(``upload_file_to_s3``, ``delete_files_from_s3``), ported.

The key is ``expenses/<report id>/<DD_MON_YYYY>_<ITEM>_<rounded amount>[_n].<ext>``; a name already
in use by another attachment gets a short unique suffix (the second of two lines alike used to
overwrite the first one's receipt); a PNG re-encoded to JPEG by the downsizing is stored under a
``.jpg`` key. Uploads retry twice (1 s, 2 s). One client per process (``settings.S3``).
"""

from __future__ import annotations

import io
import logging
import os
import time
import uuid
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings
from minty_db.models import Attachment

from petty_cash.files.downsize import downsize_bytes
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
    return boto3.client(
        "s3",
        aws_access_key_id=s3["key"],
        aws_secret_access_key=s3["secret"],
        region_name=s3["region"],
        endpoint_url=s3["endpoint_url"] or None,
        config=Config(signature_version="s3v4"),
    )


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
                   file_index=None, max_retries: int = 2) -> tuple[str, str]:
    """Store a receipt's bytes (downsized) and return ``(key, stored mime type)``."""
    key = receipt_key(report, filename, description, amount, file_index=file_index)
    downsized, out_mime = downsize_bytes(data, mime or "")
    if out_mime == "image/jpeg" and not key.lower().endswith((".jpg", ".jpeg")):
        key = os.path.splitext(key)[0] + ".jpg"
    if Attachment.objects.filter(file_path=key).exists():
        stem, extension = os.path.splitext(key)
        key = f"{stem}_{uuid.uuid4().hex[:6].upper()}{extension}"

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            client().upload_fileobj(io.BytesIO(downsized), bucket(), key)
            return key, out_mime or mime
        except Exception as exc:  # noqa: BLE001 - any transport failure is retried, then raised
            last_error = exc
            if attempt < max_retries:
                logger.warning("Retry %s/%s for %s: %s", attempt + 1, max_retries, key, exc)
                time.sleep(2**attempt)
    logger.error("Upload of %s failed after %s attempts: %s", key, max_retries + 1, last_error)
    raise last_error


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
