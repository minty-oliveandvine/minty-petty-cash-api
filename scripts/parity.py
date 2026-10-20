"""Parity: the same requests, with the same minted token, to Flask's Petty Cash API and to this
service, compared field by field - minty-onboarding-api's ``scripts/parity.py`` pattern. A route
is ported only when parity reports MATCH (or only the differences in ``EXPECTED_DIFFS``).

Both services must read the SAME database (each one's ``DATABASE_URL``). Reads only here; a
write is proved on a disposable company with ``--write`` once a route has one.

    python scripts/parity.py --flask http://127.0.0.1:5010 --django http://127.0.0.1:8010 \
        --entity <company id> --user <member's user id> [--day YYYY-MM-DD] [--share-link <row id>]

``--share-link`` also compares the PUBLIC read ``GET /public/share/<path>`` of an existing
``share_link`` row (never one made for the run): its path is read from the database here and never
printed, and it is sent only when its token passes the signature check and it has not expired -
otherwise either service would DELETE the row on reading it.

The signing key is this repo's ``.env`` ``SECRET_KEY`` (the one both services verify with); it is
never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import jwt
import requests
from dotenv import dotenv_values

#: (path suffix, json path) pairs that legitimately differ, and why.
EXPECTED_DIFFS = {
    # seconds since the draft was last edited: the two requests are not simultaneous
    ("/home", "draft.last_edit_seconds"): "timing",
    # onboarding's start date, added here for the web app's floor; Flask's /home has no such key
    ("/home", "start_date"): "added",
}


def _token(secret: str, user_id: str) -> str:
    now = int(time.time())
    return jwt.encode({"user_id": user_id, "module": "billing", "iat": now, "exp": now + 600}, secret,
                      algorithm="HS256")


def _diff(a, b, path=""):
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            sub = f"{path}.{key}" if path else str(key)
            if key not in a or key not in b:
                yield sub, a.get(key, "<missing>"), b.get(key, "<missing>")
            else:
                yield from _diff(a[key], b[key], sub)
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            yield from _diff(x, y, f"{path}[{i}]")
    elif a != b:
        yield path, a, b


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--flask", default="http://127.0.0.1:5010")
    parser.add_argument("--django", default="http://127.0.0.1:8010")
    parser.add_argument("--entity", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--day", help="transaction_date for the step reads")
    parser.add_argument("--report", action="append", default=[], help="a report id whose outcome, Daily Report and publishing status to compare")
    parser.add_argument("--share-link", action="append", default=[],
                        help="an existing, unexpired share_link row id whose public read to compare")
    parser.add_argument("--xero-settings", action="store_true",
                        help="also compare GET /settings/xero - ONLY for a company with no Xero connection: on a "
                             "connected one both services ask Xero, and a token refresh rotates a real refresh token")
    args = parser.parse_args()

    secret = dotenv_values(Path(__file__).resolve().parents[1] / ".env").get("SECRET_KEY")
    if not secret:
        print("No SECRET_KEY in .env", file=sys.stderr)
        return 2
    auth = {"Authorization": f"Bearer {_token(secret, args.user)}"}
    query = f"?transaction_date={args.day}" if args.day else ""
    reads = ["", "/home", "/settings", "/reports", "/reports?per_page=25&page=2", "/reports?status=failed"]
    if args.xero_settings:
        reads.insert(3, "/settings/xero")
    if args.day:
        reads += [f"/reports/{step}{query}" for step in ("opening", "sales", "expenses", "deposit", "cash-count",
                                                         "close")]
    for report_id in args.report:
        reads += [f"/reports/{report_id}/outcome", f"/reports/{report_id}", f"/reports/{report_id}/publishing-status"]
    reads.append("/reports/export.csv")  # no dates: the 422
    if args.day:
        # the longest range the export allows (366 days), ending on the day
        start = (date.fromisoformat(args.day) - timedelta(days=365)).isoformat()
        reads.append(f"/reports/export.csv?start_date={start}&end_date={args.day}")

    requests_to_send = [(f"/api/petty-cash/v1/companies/{args.entity}{suffix}", suffix.split("?")[0], auth)
                        for suffix in reads]
    for link_id in args.share_link:
        share_path = _share_path(link_id, secret)
        if share_path is None:
            return 2
        # no token: anyone holding the link; the route name hides the secret
        requests_to_send.append((f"/api/petty-cash/v1/public/share/{share_path}", "/public/share/<path>", {}))
        requests_to_send.append((f"/api/petty-cash/v1/public/share/{share_path.rsplit('/', 1)[0]}/{'Z' * 32}",
                                 "/public/share/<unknown secret>", {}))

    failed = False
    for path, route, send_headers in requests_to_send:
        flask = requests.get(args.flask + path, headers=send_headers, timeout=60)
        django = requests.get(args.django + path, headers=send_headers, timeout=60)
        if django.status_code == 404 and flask.status_code != 404:
            print(f"NOT PORTED {route or '/'}")
            continue
        diffs = []
        if flask.status_code != django.status_code:
            diffs.append(("<status>", flask.status_code, django.status_code))
        if "text/csv" in flask.headers.get("Content-Type", "") + django.headers.get("Content-Type", ""):
            # a file: the bytes and the two headers that name it, exactly
            if flask.content != django.content:
                diffs.append(("<bytes>", f"{len(flask.content)} bytes", f"{len(django.content)} bytes"))
            for header in ("Content-Type", "Content-Disposition"):
                if flask.headers.get(header) != django.headers.get(header):
                    diffs.append((f"<{header}>", flask.headers.get(header), django.headers.get(header)))
        else:
            try:
                diffs += list(_diff(flask.json(), django.json()))
            except ValueError:
                diffs.append(("<body>", flask.text[:80], django.text[:80]))
        unexpected = [d for d in diffs if (route, d[0]) not in EXPECTED_DIFFS]
        print(f"{'MATCH' if not unexpected else 'DIFF '} {route or '/'} ({flask.status_code})"
              + (f" - expected: {', '.join(d[0] for d in diffs if d not in unexpected)}" if len(diffs) > len(unexpected) else ""))
        for field, a, b in unexpected:
            failed = True
            print(f"    {field}: flask={json.dumps(a, default=str)[:120]}  django={json.dumps(b, default=str)[:120]}")
    return 1 if failed else 0


def _share_path(link_id: str, secret: str) -> str | None:
    """The row's path, read through this service's models (its ``.env`` database), or None when
    reading it would delete it (expired, or a token that fails the signature)."""
    import os
    from datetime import UTC, datetime

    import django

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    from petty_cash.services.share import verify_share_token
    from shared_models.models import ShareLink

    link = ShareLink.objects.filter(id=link_id).first()
    if link is None:
        print(f"No share_link {link_id}", file=sys.stderr)
        return None
    if link.expires_at <= datetime.now(UTC) or not verify_share_token(link.token, secret)[0]:
        print(f"share_link {link_id} is expired or its token fails - reading it would delete it; not sent",
              file=sys.stderr)
        return None
    return link.path_segment


if __name__ == "__main__":
    sys.exit(main())
