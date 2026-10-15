"""Parse ``S3_URL`` into the ``S3_*`` settings the attachment code reads.

    https://KEY:SECRET@s3.<region>.backblazeb2.com/<bucket>[?region=<region>]

Region comes from ``?region=`` when given, else from an ``s3.<region>.`` host, else
``us-east-1``. Key and secret are percent-decoded (a B2 secret may contain ``/`` or ``+``).
Plain Python, no Django import.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlsplit

DEFAULT_REGION = "us-east-1"
_HOST_REGION = re.compile(r"^s3\.([a-z0-9-]+)\.")


def parse_s3_url(url: str) -> dict:
    """Return ``{"endpoint_url", "bucket", "key", "secret", "region"}``; all empty but region when unset."""
    if not url:
        return {"endpoint_url": "", "bucket": "", "key": "", "secret": "", "region": DEFAULT_REGION}
    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc = host + (f":{parts.port}" if parts.port else "")
    match = _HOST_REGION.match(host)
    region = (parse_qs(parts.query).get("region") or [""])[0] or (match.group(1) if match else DEFAULT_REGION)
    return {
        "endpoint_url": f"{parts.scheme}://{netloc}" if host else "",
        "bucket": unquote(parts.path.strip("/")),
        "key": unquote(parts.username or ""),
        "secret": unquote(parts.password or ""),
        "region": region,
    }
