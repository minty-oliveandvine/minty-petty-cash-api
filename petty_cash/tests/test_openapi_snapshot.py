"""The API contract, pinned.

``petty_cash/tests/snapshots/openapi.json`` is the django-ninja schema: every path and method this
service answers. The bodies are read by hand (Flask's sentences, not schema dumps), so the snapshot
pins the routes, not the payloads - those are pinned by each step's tests and by
``scripts/parity.py`` against Flask. A route added, moved or dropped shows up here as a readable
diff instead of as a broken page in minty-petty-cash-web.

When a change is intended, regenerate with

    UPDATE_OPENAPI_SNAPSHOT=1 pytest petty_cash/tests/test_openapi_snapshot.py

and commit the new snapshot together with the frontend change that consumes it - a decision, not a
fix.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from django.test import Client

SNAPSHOT = Path(__file__).parent / "snapshots" / "openapi.json"


def _live_schema() -> dict:
    response = Client().get("/api/petty-cash/v1/openapi.json")
    assert response.status_code == 200, response.content[:300]
    return json.loads(response.content)


def _flatten(node, prefix="", out=None):
    """path -> value pairs, so a diff names the exact field that moved."""
    out = {} if out is None else out
    if isinstance(node, dict):
        for k, v in sorted(node.items()):
            _flatten(v, f"{prefix}/{k}", out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _flatten(v, f"{prefix}[{i}]", out)
    else:
        out[prefix] = node
    return out


def test_openapi_matches_the_snapshot():
    live = _live_schema()
    if os.environ.get("UPDATE_OPENAPI_SNAPSHOT") == "1":
        SNAPSHOT.write_text(json.dumps(live, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    a, b = _flatten(expected), _flatten(live)
    removed = sorted(k for k in a if k not in b)
    added = sorted(k for k in b if k not in a)
    changed = sorted(k for k in a if k in b and a[k] != b[k])
    report = []
    if removed:
        report.append("REMOVED:\n  " + "\n  ".join(removed[:40]))
    if added:
        report.append("ADDED:\n  " + "\n  ".join(added[:40]))
    if changed:
        report.append("CHANGED:\n  " + "\n  ".join(f"{k}: {a[k]!r} -> {b[k]!r}" for k in changed[:40]))
    assert not report, "the API contract moved (UPDATE_OPENAPI_SNAPSHOT=1 to accept):\n" + "\n".join(report)
