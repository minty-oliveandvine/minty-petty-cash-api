"""Parity: the same requests, with the same minted token, to Flask's Petty Cash API and to this
service, compared field by field - minty-onboarding-api's ``scripts/parity.py`` pattern. A route
is ported only when parity reports MATCH (or only the differences in ``EXPECTED_DIFFS``).

Both services must read the SAME database (each one's ``DATABASE_URL``). Reads only here; a
write is proved on a disposable company with ``--write`` once a route has one.

    python scripts/parity.py --flask http://127.0.0.1:5010 --django http://127.0.0.1:8010 \
        --entity <company id> --user <member's user id> [--day YYYY-MM-DD]

The signing key is this repo's ``.env`` ``SECRET_KEY`` (the one both services verify with); it is
never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import jwt
import requests
from dotenv import dotenv_values

#: (path suffix, json path) pairs that legitimately differ, and why.
EXPECTED_DIFFS = {
    # seconds since the draft was last edited: the two requests are not simultaneous
    ("/home", "draft.last_edit_seconds"): "timing",
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
    parser.add_argument("--report", action="append", default=[], help="a report id whose outcome and Daily Report to compare")
    args = parser.parse_args()

    secret = dotenv_values(Path(__file__).resolve().parents[1] / ".env").get("SECRET_KEY")
    if not secret:
        print("No SECRET_KEY in .env", file=sys.stderr)
        return 2
    auth = {"Authorization": f"Bearer {_token(secret, args.user)}"}
    query = f"?transaction_date={args.day}" if args.day else ""
    reads = ["", "/home", "/reports", "/reports?per_page=25&page=2", "/reports?status=failed"]
    if args.day:
        reads += [f"/reports/{step}{query}" for step in ("opening", "sales", "expenses", "deposit", "cash-count",
                                                         "close")]
    for report_id in args.report:
        reads += [f"/reports/{report_id}/outcome", f"/reports/{report_id}"]

    failed = False
    for suffix in reads:
        path = f"/api/petty-cash/v1/companies/{args.entity}{suffix}"
        flask = requests.get(args.flask + path, headers=auth, timeout=60)
        django = requests.get(args.django + path, headers=auth, timeout=60)
        route = suffix.split("?")[0]
        if django.status_code == 404 and flask.status_code != 404:
            print(f"NOT PORTED {route or '/'}")
            continue
        diffs = []
        if flask.status_code != django.status_code:
            diffs.append(("<status>", flask.status_code, django.status_code))
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


if __name__ == "__main__":
    sys.exit(main())
