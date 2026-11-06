"""Publishing a report to Xero: the start, then the work in the background.

Ported from Flask in Phase 3 of the Flask retirement (2026-11-05). It replaces the call to Flask's
``POST /api/internal/xero/publish``. The Flask sources:
- ``xero/services/publish_start.py``: ``start_publish``;
- ``xero/services/publish.py``: ``process_xero_integration_background``.

**The start**, in Flask's order:
1. the Xero token (accounts-api's token service);
2. the company's Xero settings;
3. a connection re-sync when the company isn't marked connected;
4. the report (never a draft);
5. the system-account check;
6. ``SELECT ... FOR UPDATE NOWAIT`` and ``publishing_status = publishing``, so a second click is a
   409. Then the worker starts on its own thread.

**The worker** puts the report into Xero (``petty_cash/xero/steps.py``). After a failed publish it
retries only the parts that failed. Then it stamps the report:
- **completed:** ``status = published`` and ``xero_integrated`` on;
- **failed:** a ``publish_failed`` history row holding the reasons.

It never leaves a report ``publishing``.

Deliberate differences from Flask:
- The worker asks for its own token rather than reusing the start's.
- A worker that can't get a token writes a reason ("Xero connection has expired" / "could not reach
  Xero"); Flask wrote none, so the page showed a failure with no reason.
- The history rows name the person who published; Flask's named the company's connector.
"""

from __future__ import annotations

import json
import logging
import threading

import requests
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.utils import timezone
from minty_db.models import Entity, Report, ReportHistory
from minty_shared import logctx

from core.xero_tokens import XeroTokenUnavailable, xero_token
from petty_cash.services.publish_reasons import latest_reason_items
from petty_cash.services.reports import currency_code
from petty_cash.services.xero_settings import settings_complete
from petty_cash.xero import errors as pub_err
from petty_cash.xero.steps import Run, publish_report, system_account_errors

logger = logging.getLogger("petty-cash-api")

STARTED = "Publishing to Xero. This can take a few minutes."
ALREADY = "Publishing already in progress. Please wait..."
DRAFT = "This report is still a draft. Submit the day before publishing it."
RECONNECT = "I've lost access to your Xero account. Mind reconnecting it in Settings?"
NO_TOKEN_SERVICE = "I couldn't reach Xero to start the publish. Try again in a moment."
INCOMPLETE = "Xero settings incomplete. Please configure entity settings first."
CONNECTIONS_URL = "https://api.xero.com/connections"


class PublishRefused(Exception):
    """A publish that can't start: the sentence, the status, and whether Xero must be connected again."""

    def __init__(self, message: str, status: int, *, reconnect: bool = False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.reconnect = reconnect


def sync_connection_status(entity: Entity, access_token: str) -> None:
    """The company's ``status`` from Xero's own list of connections (Flask's
    ``sync_entity_xero_status``): connected when its organisation is listed, disconnected when not.
    Xero not answering keeps the status as it is (logged)."""
    try:
        response = requests.get(CONNECTIONS_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=(10, 30))
    except requests.RequestException as exc:
        logger.warning("Publish: Xero connections unreachable for company %s: %s", entity.id, exc)
        return
    if response.status_code != 200:
        logger.warning("Publish: Xero connections answered %s for company %s; status kept", response.status_code,
                       entity.id)
        return
    try:
        listed = any(c.get("tenantId") == str(entity.xero_org_id) for c in response.json() or [])
    except (ValueError, AttributeError):
        logger.warning("Publish: Xero connections answer unreadable for company %s; status kept", entity.id)
        return
    if listed and entity.status != "connected":
        Entity.objects.filter(id=entity.id).update(status="connected", last_connected_at=timezone.now())
        logger.info("Publish: company %s is connected in Xero; status updated", entity.id)
    elif not listed and entity.status != "disconnected":
        Entity.objects.filter(id=entity.id).update(status="disconnected")
        logger.info("Publish: company %s is no longer connected in Xero; status updated", entity.id)


def start_publish(entity_id, report: Report, person) -> dict:
    """Start the publish of ``report`` (this company's, viewable, ``REPORT_PUBLISH`` - checked by the
    route). The 202 answer; raises ``PublishRefused``."""
    entity = Entity.objects.get(id=entity_id)
    try:
        token = xero_token(entity_id)
    except XeroTokenUnavailable as unavailable:
        if unavailable.reconnect:
            raise PublishRefused(RECONNECT, 400, reconnect=True) from None
        raise PublishRefused(NO_TOKEN_SERVICE, 503) from None
    if not settings_complete(entity_id):
        raise PublishRefused(INCOMPLETE, 400)
    if entity.status != "connected":
        if entity.status == "disconnected":
            logger.warning("Publish: company %s is marked disconnected; trying its stored token", entity_id)
        sync_connection_status(entity, token.access_token)
    if report.status == "draft":
        raise PublishRefused(DRAFT, 409)
    errors = system_account_errors(entity_id, report.id)
    if errors:
        if len(errors) == 1:
            message = errors[0]["message"]
        else:
            listed = "\n".join(f"• {e['expense']} - {e['account_code']}" for e in errors)
            message = f"System accounts detected:\n{listed}\n\nPlease update."
        raise PublishRefused(message, 400)

    try:
        with transaction.atomic():
            locked = Report.objects.select_for_update(nowait=True).get(id=report.id)
            if locked.publishing_status == "publishing":
                raise PublishRefused(ALREADY, 409)
            # Before the flip: the worker retries only the failed parts of a failed publish.
            prior = locked.publishing_status
            locked.publishing_status = "publishing"
            locked.save(update_fields=["publishing_status"])
    except DatabaseError as exc:  # NOWAIT: another start holds the row
        logger.warning("Publish: report %s is locked by another start: %s", report.id, exc)
        raise PublishRefused(ALREADY, 409) from None

    try:
        threading.Thread(target=logctx.carry_context(_in_background),
                         args=(entity_id, report.id, getattr(person, "id", None), prior), daemon=True).start()
    except Exception:
        _unstick(report.id)
        raise
    logger.info("Publish: started for report %s", report.id)
    return {"publishing_status": "publishing", "message": STARTED}


def _unstick(report_id) -> None:
    """Never leave a report ``publishing``: nothing could then publish, edit or delete it."""
    try:
        Report.objects.filter(id=report_id, publishing_status="publishing").update(publishing_status="failed")
    except DatabaseError:
        logger.exception("Publish: could not put report %s back to failed", report_id)


def _retry_filter(report: Report, prior) -> dict | None:
    """After a failed publish, only the parts that failed; None (everything) when they can't be told."""
    if prior != "failed":
        return None
    targets = pub_err.retry_targets_from_items(latest_reason_items(report))
    if targets["unmappable"] or not (targets["modules"] or targets["expense_ids"]):
        logger.warning("Publish: selective re-publish unavailable for report %s; publishing everything", report.id)
        return None
    logger.info("Publish: selective re-publish for report %s: %s", report.id, targets)
    return {"modules": targets["modules"], "expense_ids": targets["expense_ids"]}


def _finish(report_id, user_id, result: dict) -> None:
    with transaction.atomic():
        report = Report.objects.select_for_update().get(id=report_id)
        if result.get("failed", 1) == 0:
            report.xero_integrated = True
            report.publishing_status = "completed"
            report.status = "published"
            report.published_at = timezone.now()
            ReportHistory.objects.create(report_id=report.id, user_id=user_id, action="published")
            logger.info("Publish: report %s is in Xero", report.id)
        else:
            report.xero_integrated = False
            report.publishing_status = "failed"
            ReportHistory.objects.create(report_id=report.id, user_id=user_id,
                                         action=pub_err.PublishFailureReason.HISTORY_ACTION_LABEL,
                                         new_value=json.dumps(result.get("reason_items") or []))
            logger.warning("Publish: report %s %s: %s ok, %s failed: %s", report.id,
                           "partly published" if result.get("succeeded") else "not published",
                           result.get("succeeded", 0), result.get("failed"), result.get("reasons"))
        report.save()


def _in_background(*args) -> None:
    """The worker thread: its own database connection, closed when it ends."""
    close_old_connections()
    try:
        run_publish(*args)
    finally:
        connection.close()


def run_publish(entity_id, report_id, user_id, prior) -> None:
    """The background publish of one report."""
    try:
        report = Report.objects.filter(id=report_id).first()
        if report is None:
            logger.error("Publish: report %s is gone", report_id)
            return
        entity = Entity.objects.get(id=entity_id)
        try:
            token = xero_token(entity_id)
        except XeroTokenUnavailable as unavailable:
            reason = pub_err.XERO_AUTH_EXPIRED if unavailable.reconnect else "could not reach Xero, please try again"
            _finish(report_id, user_id, {"failed": 1, "succeeded": 0, "reasons": [reason],
                                         "reason_items": [{"text": reason}]})
            return
        run = Run(entity=entity, access_token=token.access_token, org=str(token.xero_org_id or entity.xero_org_id),
                  currency=currency_code(entity_id))
        result = publish_report(run, report, _retry_filter(report, prior))
        _finish(report_id, user_id, result)
    except Exception:
        logger.exception("Publish: the background publish failed for report %s", report_id)
    finally:
        _unstick(report_id)
