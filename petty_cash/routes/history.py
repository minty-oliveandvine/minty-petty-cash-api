"""Report history - Flask's ``petty_cash_api/routes/history.py``.

``GET /reports?status=&start_date=&end_date=&page=&per_page=``: one page of the company's reports,
newest day first, each with its Status pill, its figures, who submitted it, a failed publish's
reasons (``services/publish_reasons.py``, worked out here since Phase 3 of the Flask retirement,
2026-11-05) and what this person may do to it. A bad filter is refused (422) with a sentence.

``GET /reports/export.csv?start_date=&end_date=``: the movements CSV of the submitted reports in the
range (``services/report_csv.py``, Flask's ``report_csv.py`` byte for byte) as a file. Both dates,
``YYYY-MM-DD``, start on or before end, at most a year (366 days); a bad range is 422, nothing to
export 404. With neither date it is the company's whole history (owner, 2026-11-13; Flask refused
it), named by its first and last day; one date alone is still 422. Static, and the report router is
mounted last, so ``/reports/{report_id}`` never
captures it.

``GET /reports/petty-cash-report.csv?start_date=&end_date=``: the Petty Cash Report beside it
(``services/petty_cash_report_csv.py``; owner, 2026-11-13) - each submitted day's report, then its
expense lines with their account and supplier. Same access, dates (optional: the whole history
with neither), refusals and headers as the movements CSV.
"""

from __future__ import annotations

from datetime import date

from django.http import HttpResponse
from minty_db.enums import ReportStatus
from ninja import Router

from core.exceptions import Refused
from core.policy import Permission, can_delete_report, has_permission
from petty_cash.days import hk
from petty_cash.money import out
from petty_cash.routes.close import _iso
from petty_cash.services import petty_cash_report_csv, report_csv
from petty_cash.services.history import STATUS_FILTERS, report_history
from petty_cash.services.publish_reasons import publish_reasons
from petty_cash.services.report_actions import was_published
from petty_cash.services.report_page import report_status
from petty_cash.services.reports import currency_code, last_submitted
from petty_cash.services.xero_settings import minty_on_its_own

router = Router()

PER_PAGE = (10, 25, 50, 100)
#: The export's sentence for each refused range (``report_csv.parse_range``'s reasons) - Flask's.
EXPORT_REFUSALS = {
    "missing": "Choose both dates to export the reports between them.",
    "format": "A date isn't in the YYYY-MM-DD form. Choose it from the calendar.",
    "order": "The start date is after the end date. Choose a start on or before the end.",
    "too_long": "The range is longer than a year. Choose 366 days or fewer.",
}
NO_EXPORT_ACCESS = "You don't have permission to export this company's reports."
NOTHING_TO_EXPORT = "There are no submitted reports between those dates. Choose another range."
NOTHING_SUBMITTED = "There are no submitted reports to export yet."


def _filters(args) -> dict:
    """The query's filters, or a 422 with the sentence that refuses them."""
    status = args.get("status") or None
    if status is not None and status not in STATUS_FILTERS:
        raise Refused("That status filter isn't one I know. Choose Draft, Submitted, Published or Failed.", 422)
    try:
        page = int(args.get("page") or 1)
        per_page = int(args.get("per_page") or 10)
    except ValueError:
        raise Refused("The page or the page size isn't a whole number. Choose a page from the list.", 422) from None
    if page < 1:
        raise Refused("Pages start at 1. Choose a page from the list.", 422)
    if per_page not in PER_PAGE:
        raise Refused("Show 10, 25, 50 or 100 reports a page.", 422)
    start, end = args.get("start_date") or "", args.get("end_date") or ""
    if bool(start) != bool(end):
        raise Refused("Choose both dates to see the reports between them.", 422)
    try:
        days = [date.fromisoformat(d) for d in (start, end) if d]
    except ValueError:
        raise Refused("A date isn't in the YYYY-MM-DD form. Choose it from the calendar.", 422) from None
    if days and days[0] > days[1]:
        raise Refused("The start date is after the end date. Choose a start on or before the end.", 422)
    return {"status": status, "page": page, "per_page": per_page,
            "start_date": days[0] if days else None, "end_date": days[1] if days else None}


@router.get("/reports")
def history(request):
    filters = _filters(request.GET)
    entity_id, person = request.company.id, request.auth_user
    data = report_history(entity_id, **filters)
    failed = [r.id for r in data["reports"] if r.publishing_status == "failed"]
    reasons = publish_reasons(entity_id, failed) if failed else {}
    may_edit = has_permission(person, Permission.REPORT_EDIT_OWN, entity_id)
    latest_id = data["latest_report_id"]
    last = last_submitted(entity_id)  # a draft dated before it is stale: deletable, never latest

    rows = []
    for report in data["reports"]:
        draft = report.status == ReportStatus.DRAFT
        latest = str(report.id) == latest_id
        stale = draft and last is not None and report.transaction_date < last.transaction_date
        label, tone = report_status(report)
        rows.append({
            "id": str(report.id),
            "transaction_date": report.transaction_date.isoformat(),
            "status": str(report.status or "submitted"),
            "status_label": label,
            "status_tone": tone,
            # where a draft resumes (opening .. ending), as the dashboard's draft has it
            "current_section": report.current_section,
            "total_sales": out(report.total_sales),
            "total_expenses": out(report.expense_total),
            "submitted_by": data["first_names"].get(report.created_by) or None,
            "published_by": data["first_names"].get(data["publishers"].get(report.id)) or None,
            "created_at": _iso(hk(report.created_at)),  # when the day was started - a draft's only time
            "submitted_at": _iso(hk(report.submitted_at)),
            "published_at": _iso(hk(report.published_at)),
            # a row already publishing when the list loads can be watched (GET .../publishing-status)
            "publishing_status": str(report.publishing_status) if report.publishing_status is not None else None,
            # it has been to Xero, even if edited since: the delete warns (as GET /reports/{id})
            "was_published": was_published(report),
            "publish_reasons": reasons.get(str(report.id), []),
            "permissions": {
                "edit": latest and not draft and may_edit,
                "delete": ((latest and not draft) or stale) and can_delete_report(person, report),
            },
        })
    return {
        "reports": rows,
        "page": filters["page"],
        "per_page": filters["per_page"],
        "total_pages": data["total_pages"],
        "total_count": data["total_count"],
        "latest_report_id": latest_id,
        "currency_code": currency_code(entity_id),
        "can_publish": has_permission(person, Permission.REPORT_PUBLISH, entity_id)
        and not minty_on_its_own(entity_id),  # Xero optional: nowhere to publish to
    }


def _export_reports(request, entity_id):
    """The reports an export covers, the days that name its file and the 404 sentence when it holds
    nothing: both dates' range, or with neither the whole history. A bad range is 422."""
    start, end = request.GET.get("start_date"), request.GET.get("end_date")
    if not start and not end:
        reports = report_csv.every_submitted_report(entity_id)
        if not reports:
            raise Refused(NOTHING_SUBMITTED, 404)
        return reports, reports[0].transaction_date, reports[-1].transaction_date, NOTHING_SUBMITTED
    try:
        first, last = report_csv.parse_range(start, end)
    except report_csv.RangeRefused as refused:
        raise Refused(EXPORT_REFUSALS[refused.reason], 422) from None
    reports = report_csv.submitted_reports(entity_id, first, last)
    if not reports:
        raise Refused(NOTHING_TO_EXPORT, 404)
    return reports, first, last, NOTHING_TO_EXPORT


@router.get("/reports/export.csv")
def export_csv(request):
    entity_id = request.company.id
    if not report_csv.may_export(request.auth_user, entity_id):
        raise Refused(NO_EXPORT_ACCESS, 403)
    reports, first, last, nothing = _export_reports(request, entity_id)
    rows = report_csv.movements(entity_id, reports)
    if not rows:  # submitted days with no money movement at all
        raise Refused(nothing, 404)
    # Access-Control-Expose-Headers: Content-Disposition is settings.CORS_EXPOSE_HEADERS
    resp = HttpResponse(report_csv.to_csv(rows), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = report_csv.export_disposition(request.company.name, first, last)
    return resp


@router.get("/reports/petty-cash-report.csv")
def export_petty_cash_report(request):
    entity_id = request.company.id
    if not report_csv.may_export(request.auth_user, entity_id):
        raise Refused(NO_EXPORT_ACCESS, 403)
    reports, first, last, _ = _export_reports(request, entity_id)
    body = petty_cash_report_csv.rows(reports, currency_code(entity_id))
    # Access-Control-Expose-Headers: Content-Disposition is settings.CORS_EXPOSE_HEADERS
    resp = HttpResponse(petty_cash_report_csv.to_csv(body), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = report_csv.export_disposition(
        request.company.name, first, last, label=petty_cash_report_csv.FILE_LABEL)
    return resp
