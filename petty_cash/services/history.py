"""Report history's list - Flask's ``blueprints/report/services/history_query.py::
get_entity_report_history``, ported: one page of a company's reports, newest day first, filtered by
one Status pill and a date range, counted and cut in SQL. A failed report's publish reasons are not
worked out here (they may ask Xero): ``core/accounts_api.py::publish_reasons`` asks Flask, for the
failed rows on the page only.
"""

from __future__ import annotations

from django.db.models import Q
from minty_db.enums import ReportStatus
from minty_db.models import Report, User

#: The history page's filter chips -> the Status pills each keeps (``report_page.report_status``).
STATUS_FILTERS = ("draft", "submitted", "published", "failed")
WORKING = ("failed", "publishing")


def _status_q(status: str) -> Q:
    """``report_status()``'s pills as SQL, so the filter runs before the page is cut. ``status``
    and ``publishing_status`` are NOT NULL, so a negation keeps Flask's meaning."""
    draft = Q(status=ReportStatus.DRAFT)
    in_xero = Q(xero_integrated=True)
    not_in_xero = Q(xero_integrated__isnull=True) | Q(xero_integrated=False)
    working = Q(publishing_status__in=WORKING)
    return {
        "draft": draft,
        "published": ~draft & in_xero,
        "failed": ~draft & not_in_xero & working,
        "submitted": ~draft & not_in_xero & ~working,
    }[status]


def report_history(entity_id, *, status=None, start_date=None, end_date=None, page=1, per_page=10) -> dict:
    """``{reports, first_names, total_count, total_pages, latest_report_id}`` - ``first_names`` maps
    a creator's id to their first name (one query for the page)."""
    reports = Report.objects.filter(entity_id=entity_id)
    if start_date and end_date:
        reports = reports.filter(transaction_date__gte=start_date, transaction_date__lte=end_date)
    if status:
        reports = reports.filter(_status_q(status))
    total_count = reports.count()
    total_pages = (total_count + per_page - 1) // per_page if total_count else 1
    rows = list(reports.order_by("-transaction_date")[(page - 1) * per_page:page * per_page])
    first_names = dict(User.objects.filter(id__in={r.created_by for r in rows if r.created_by})
                       .values_list("id", "first_name"))
    # The newest day of any status, a draft included.
    latest = Report.objects.filter(entity_id=entity_id).order_by("-transaction_date").values_list("id", flat=True).first()
    return {"reports": rows, "first_names": first_names, "total_count": total_count, "total_pages": total_pages,
            "latest_report_id": str(latest) if latest else None}
