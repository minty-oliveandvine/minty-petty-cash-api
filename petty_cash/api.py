"""Every router, in the order Flask's ``petty_cash_api/routes/__init__.py`` loads them. Each is
mounted under ``/companies/{entity_id}`` (``config/urls.py``); the door (``core/auth.py``) reads
the company from that address. The one exception is ``public_share`` under ``/public``: a shared
Daily Report, its single operation ``auth=None`` (Flask's ``petty_cash_public_bp``)."""

from petty_cash.routes import (
    cash,
    close,
    company,
    expenses,
    history,
    home,
    opening,
    public_share,
    report,
    sales,
    settings,
)

COMPANY = "/companies/{entity_id}"

routers = [
    (COMPANY, company.router, "Company"),
    (COMPANY, home.router, "Dashboard"),
    (COMPANY, opening.router, "Opening"),
    (COMPANY, sales.router, "Sales"),
    (COMPANY, expenses.router, "Expenses"),
    (COMPANY, cash.router, "Cash"),
    (COMPANY, close.router, "Close"),
    (COMPANY, history.router, "History"),
    (COMPANY, settings.router, "Settings"),
    # LAST: /reports/{report_id} must not answer for /reports/opening and the other steps.
    (COMPANY, report.router, "Report"),
    # no company and no token: the share link's secret is the key (tests/test_public_share.py)
    (public_share.PREFIX, public_share.router, "Public share"),
]
