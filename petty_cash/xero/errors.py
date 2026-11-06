"""Why a Xero publish failed, in words a person can act on - Flask's
``blueprints/xero/services/publish_errors.py``, copied unchanged in Phase 3 of the Flask retirement
(2026-11-05): it is plain Python, and Flask's publish and this one write the same
``report_history`` items until the release. Its own notes follow.


The Xero publish pipeline (blueprints/xero/services/publish.py) historically
swallowed *why* a publish failed and only persisted a binary
``Report.publishing_status='failed'`` flag.  The frontend then showed a
generic "Publishing to Xero failed. Please try again." toast that did not
help the user fix the underlying configuration.

This module exposes a small ``PublishFailureReason`` accumulator that the
helper functions populate as they detect specific missing configuration
(e.g. no petty-cash account mapping, no cashsale contact).  The orchestrator
attaches it to the module response, the background worker writes it to a
``ReportHistory`` row prefixed ``publish_failed:``, and the publishing-status
API surfaces it back to the report-history page so the toast tells the user
exactly which Xero settings to complete.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

logger = logging.getLogger("petty-cash-api")

# Sentinel reason emitted when Xero rejects the OAuth token (HTTP 401).  An
# expired/revoked token is an account-wide condition, not a per-module one, so
# the publish orchestrator collapses it to a single reason instead of repeating
# it once per module/expense line.  Keep this exact wording — the orchestrator
# and the report-history toast both key off it.
XERO_AUTH_EXPIRED = "Xero connection has expired"

# Republishing a report now UPDATES the entries it created rather than posting
# duplicates, which means Xero can refuse where it previously just accepted a
# second copy. These two are the refusals a user can actually act on, so they
# get named wording instead of Xero's raw validation text.
XERO_RECONCILED = (
    "this entry is already reconciled in Xero, so it cannot be changed there. "
    "Unreconcile it in Xero, then publish again"
)
XERO_HAS_PAYMENT = (
    "this entry already has a payment or credit note against it in Xero, so it "
    "cannot be changed. Remove that in Xero, then publish again"
)


def _latest_publish_failed_row(histories):
    """Return the most recent publish-failed ReportHistory-like row, or None."""
    rows = sorted(
        list(histories or []),
        key=lambda r: getattr(r, "timestamp", None) or datetime.min,
        reverse=True,
    )
    for row in rows:
        if PublishFailureReason.reason_items_from_history(
            getattr(row, "action", None), getattr(row, "new_value", None)
        ):
            return row
    return None


def latest_publish_reasons(histories) -> list[str]:
    """Return the plain-English reasons from the most recent publish-failed row.

    ``histories`` is any iterable of ReportHistory-like rows exposing
    ``action``, ``new_value`` and ``timestamp``.  Returns ``[]`` when no
    publish-failed row is present.
    """
    row = _latest_publish_failed_row(histories)
    if not row:
        return []
    return PublishFailureReason.reasons_from_history(
        getattr(row, "action", None), getattr(row, "new_value", None)
    )


def latest_publish_reason_items(histories) -> list[dict]:
    """Return the structured failure items from the most recent publish-failed row.

    Each item is a dict with at least ``text`` and, for new rows, resolution
    metadata (``scope``/``expense_id``/``deps``).  Returns ``[]`` when none.
    """
    row = _latest_publish_failed_row(histories)
    if not row:
        return []
    return PublishFailureReason.reason_items_from_history(
        getattr(row, "action", None), getattr(row, "new_value", None)
    )


# Stable orchestrator module keys (mirror xero_integrated_module's dispatch).
_VALID_MODULE_KEYS = {
    "withdrawal_from", "invoices", "expenses", "deposit", "discrepancy",
}


def _module_key_from_text(text: str):
    """Best-effort map a reason text (legacy rows) to an orchestrator module key.

    Returns a key, or None when it can't be determined unambiguously.
    """
    t = (text or "").lower()
    if "expense" in t:  # "Expense 'X' — …" (line) or "Expenses — …" (module)
        return "expenses"
    if "cash sale" in t:  # "Cash sales — …" or "cash sale contact is not set up …"
        return "invoices"
    if "discrepancy" in t:
        return "discrepancy"
    if "withdrawal" in t or "director" in t:
        return "withdrawal_from"
    if t.startswith("deposit"):
        return "deposit"
    # Ambiguous shared mappings (petty cash / operating bank are used by both
    # deposit and withdrawal) are intentionally left unmapped.
    return None


def retry_targets_from_items(items) -> dict:
    """Compute which modules/expenses to re-publish from stored failure items.

    Returns ``{"modules": set[str], "expense_ids": set[str], "unmappable": bool}``.
    ``unmappable`` is True when any failed item can't be tied to a concrete
    module/expense — the caller should then fall back to a full re-run so an
    unpublished part is never silently skipped.
    """
    modules: set[str] = set()
    expense_ids: set[str] = set()
    unmappable = False

    for item in items or []:
        if not isinstance(item, dict):
            unmappable = True
            continue
        if item.get("scope") == "expense":
            expense_id = item.get("expense_id")
            if expense_id:
                expense_ids.add(expense_id)
                modules.add("expenses")
            else:
                unmappable = True  # an expense failed but we can't target it
            continue
        key = item.get("module")
        if key not in _VALID_MODULE_KEYS:
            key = _module_key_from_text(item.get("text"))
        if key in _VALID_MODULE_KEYS:
            modules.add(key)
        else:
            unmappable = True

    return {"modules": modules, "expense_ids": expense_ids, "unmappable": unmappable}


def translate_xero_error(
    status_code: int | None,
    response_text: str | None,
    *,
    subject: str | None = None,
) -> str:
    """Translate a Xero API error response into a short, plain-English reason.

    The publish helpers already log the raw ``response.text`` from Xero; this
    turns that technical body into a user-facing phrase with no status codes,
    field names, or Xero jargon.  ``subject`` (``"contact"`` / ``"account"``)
    lets the message read naturally for the failing piece.

    Xero validation errors usually look like::

        {"Elements": [{"ValidationErrors": [{"Message": "..."}]}]}

    while problem-style errors look like ``{"Detail": "..."}`` /
    ``{"Title": "..."}``.  We pull whatever message is present, lower-case it,
    and keyword-map to a friendly phrase.
    """
    who = subject if subject in ("contact", "account") else None

    # ── Status-code-first cases (no useful body needed) ──────────────────────
    if status_code == 401:
        return XERO_AUTH_EXPIRED
    if status_code == 403:
        return "no permission to post this in Xero"
    if status_code == 429:
        return "Xero is busy, please try again shortly"

    # ── Pull a message out of the Xero error body ────────────────────────────
    message = ""
    if response_text:
        try:
            body = json.loads(response_text)
            parts: list[str] = []
            if isinstance(body, dict):
                elements = body.get("Elements")
                if isinstance(elements, list):
                    for el in elements:
                        if not isinstance(el, dict):
                            continue
                        for ve in el.get("ValidationErrors", []) or []:
                            if isinstance(ve, dict) and ve.get("Message"):
                                parts.append(str(ve["Message"]))
                for key in ("Detail", "Title", "Message"):
                    if body.get(key):
                        parts.append(str(body[key]))
            message = " ".join(parts)
        except (ValueError, TypeError):
            message = response_text
    lowered = message.lower()

    # ── Keyword mapping → plain English ──────────────────────────────────────
    # Reconciliation is checked first: it is the most common reason a petty
    # cash republish is refused (entries get reconciled quickly), and it is the
    # one the user can resolve themselves.
    if "reconcil" in lowered:
        return XERO_RECONCILED
    if ("payment" in lowered or "credit note" in lowered) and (
        "cannot" in lowered or "allocated" in lowered or "not be" in lowered
    ):
        return XERO_HAS_PAYMENT
    if "archived" in lowered:
        if who == "account" or "account" in lowered:
            return "account is archived in Xero"
        return "contact is archived in Xero"
    if "account code" in lowered or ("account" in lowered and "valid" in lowered):
        return "account is no longer active in Xero"
    if "contact" in lowered and (
        "not found" in lowered or "does not exist" in lowered or "could not be found" in lowered
    ):
        return "contact no longer exists in Xero"
    if "token" in lowered or "unauthor" in lowered:
        return XERO_AUTH_EXPIRED

    # ── Fallbacks ────────────────────────────────────────────────────────────
    # A short Xero validation sentence is genuinely useful to the person
    # fixing their mapping, so it is passed through -- but only when it reads
    # as prose. Serialised bodies, markup and stack text are not.
    if message and len(message) <= 120 and _reads_as_prose(message):
        return message.strip()
    return "Xero rejected this entry"


# Shapes that mean the "message" pulled out of Xero is machinery, not a
# sentence: a serialised body, markup, or exception text. Any of these reaching
# the toast reads as a leak, however short the string is.
_NON_PROSE_MARKERS = (
    "{", "}", "[", "<", ">", "Traceback", "Exception", "at Xero.", "System.",
    "null,", "\n", "\\n",
)


def _reads_as_prose(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    if any(marker in text for marker in _NON_PROSE_MARKERS):
        return False
    # A bare identifier or GUID is not a sentence.
    return " " in text


# Stable, user-facing labels for each Xero-mapping piece the publish flow
# depends on.  Keep these short — they appear inline in the toast body.
LABEL_PETTYCASH_ACCOUNT = "petty cash bank account"
LABEL_BANK_ACCOUNT = "operating bank account"
LABEL_CASH_SALE_ACCOUNT = "cash sale account"
LABEL_DISCREPANCY_ACCOUNT = "discrepancy account"
LABEL_DIRECTOR_ACCOUNT_CODE = "director account code"
LABEL_CASHSALE_CONTACT = "cash sale contact"
LABEL_DIRECTOR_CONTACT = "director contact"


class PublishFailureReason:
    """Accumulator for human-readable reasons why a Xero publish failed.

    Each recorded failure is kept as a structured *item* so the report-history
    page can later re-check whether that specific issue has been resolved
    (e.g. the user edited an expense to use a valid, non-archived contact).
    An item is a dict::

        {"text": "Expense 'Coffee' — contact is archived in Xero",
         "scope": "expense" | "entity" | None,
         "expense_id": "<shop_expense.id>" | None,   # scope == "expense"
         "deps": [["contact", "cashsale_contact"], ["account", "cash_sale"]]}

    ``deps`` lists the (kind, role) Xero mappings a module depends on so the
    resolver can confirm all of them are valid again.  The class is side-effect
    free; callers pass a single instance through helpers as an out-parameter.
    """

    def __init__(self) -> None:
        self._items: list[dict] = []
        self._seen_text: set[str] = set()
        self._module_index: dict[str, int] = {}

    # ── Recording ──────────────────────────────────────────────────────────

    def add_missing(self, label: str, *, deps=None, module=None) -> None:
        """Record a missing Xero-mapping piece.  De-duplicated, order-preserving."""
        if not label:
            return
        text = f"{label} is not set up in Xero settings"
        if text in self._seen_text:
            return
        self._seen_text.add(text)
        self._items.append(
            {
                "text": text,
                "scope": "entity",
                "expense_id": None,
                "deps": deps or [],
                "module": module,
            }
        )

    def add_module_error(
        self, module_label: str, reason: str, *, scope=None, expense_id=None,
        deps=None, failed_contact_id=None, module=None
    ) -> None:
        """Record a module-level failure (e.g. Xero API 4xx).

        ``scope``/``expense_id``/``deps``/``failed_contact_id``/``module`` let
        the report-history page re-check resolution and selectively re-publish
        later.  ``module`` is the stable orchestrator key (e.g. ``invoices``);
        ``module_label`` is the human prefix (e.g. ``Cash sales``).  Last writer
        wins per ``module_label``.
        """
        if not module_label or not reason:
            return
        text = reason if module_label in ("Publish",) else f"{module_label} — {reason}"
        item = {
            "text": text,
            "scope": scope,
            "expense_id": expense_id,
            "deps": deps or [],
            "failed_contact_id": failed_contact_id,
            "module": module,
        }
        if module_label in self._module_index:
            idx = self._module_index[module_label]
            self._seen_text.discard(self._items[idx]["text"])
            self._items[idx] = item
        else:
            self._module_index[module_label] = len(self._items)
            self._items.append(item)
        self._seen_text.add(text)

    # ── Inspection ─────────────────────────────────────────────────────────

    def has_any(self) -> bool:
        return bool(self._items)

    def has_auth_expired(self) -> bool:
        """True when any recorded reason is the account-wide 401 (expired token).

        The reason is stored either bare (``"Xero connection has expired"``) or
        with a module prefix (``"Withdrawal — Xero connection has expired"``), so
        we match on the suffix.  The orchestrator uses this to stop the run and
        report the expiry once instead of once per module.
        """
        return any(
            it.get("text", "").endswith(XERO_AUTH_EXPIRED) for it in self._items
        )

    # ── Rendering ──────────────────────────────────────────────────────────

    # The action column is ``String(50)`` in the ReportHistory model, so the
    # short label below MUST stay under 50 chars.  Detailed reasons are
    # written separately into ``new_value`` (Text) so we never truncate.
    HISTORY_ACTION_LABEL = "publish_failed"

    def to_reason_bullets(self) -> list[str]:
        """Return one plain-English reason per failed thing (toast/log)."""
        return [it["text"] for it in self._items]

    def to_reason_items(self) -> list[dict]:
        """Return the structured failure items (stored in ReportHistory.new_value)."""
        return [dict(it) for it in self._items]

    @classmethod
    def reason_items_from_history(
        cls, action: str | None, new_value: str | None
    ) -> list[dict]:
        """Return the stored failure items as dicts (with resolution metadata).

        New rows store a JSON array of item dicts; older rows store a JSON array
        of plain strings; legacy rows store a single string.  Items without
        metadata come back as ``{"text": ...}`` (no resolution tracking).
        """
        if not cls.is_publish_failed_action(action):
            legacy = cls.reason_from_history(action, new_value)
            return [{"text": legacy}] if legacy else []
        if not new_value:
            return []
        try:
            parsed = json.loads(new_value)
        except (ValueError, TypeError):
            stripped = new_value.strip()
            return [{"text": stripped}] if stripped else []
        if not isinstance(parsed, list):
            stripped = str(parsed).strip()
            return [{"text": stripped}] if stripped else []
        items: list[dict] = []
        for el in parsed:
            if isinstance(el, dict):
                text = str(el.get("text", "")).strip()
                if text:
                    items.append(
                        {
                            "text": text,
                            "scope": el.get("scope"),
                            "expense_id": el.get("expense_id"),
                            "deps": el.get("deps") or [],
                            "failed_contact_id": el.get("failed_contact_id"),
                            "module": el.get("module"),
                        }
                    )
            else:
                stripped = str(el).strip()
                if stripped:
                    items.append({"text": stripped})
        return items

    @classmethod
    def reasons_from_history(
        cls, action: str | None, new_value: str | None
    ) -> list[str]:
        """Return the stored reasons as a list of plain-English bullets."""
        return [
            it["text"]
            for it in cls.reason_items_from_history(action, new_value)
            if it.get("text")
        ]

    @classmethod
    def is_publish_failed_action(cls, action: str | None) -> bool:
        """Return True when an action row represents a publish failure."""
        if not action:
            return False
        return action.strip() == cls.HISTORY_ACTION_LABEL

    @classmethod
    def reason_from_history(
        cls, action: str | None, new_value: str | None
    ) -> str | None:
        """Pull a single human-readable reason string from a stored row."""
        if not cls.is_publish_failed_action(action):
            # Backward-compatible: legacy rows used to embed the reason in
            # the action column with a "publish_failed:" prefix.
            if action and action.startswith("publish_failed:"):
                return action[len("publish_failed:"):].strip() or None
            return None
        reasons = cls.reasons_from_history(action, new_value)
        return "; ".join(reasons) if reasons else None
