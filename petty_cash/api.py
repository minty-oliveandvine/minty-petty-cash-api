"""Every router, in the order Flask's ``petty_cash_api/routes/__init__.py`` loads them. Each is
mounted under ``/companies/{entity_id}`` (``config/urls.py``); the door (``core/auth.py``) reads
the company from that address."""

from petty_cash.routes import cash, close, company, expenses, home, opening, report, sales

COMPANY = "/companies/{entity_id}"

routers = [
    (COMPANY, company.router, "Company"),
    (COMPANY, home.router, "Dashboard"),
    (COMPANY, opening.router, "Opening"),
    (COMPANY, sales.router, "Sales"),
    (COMPANY, expenses.router, "Expenses"),
    (COMPANY, cash.router, "Cash"),
    (COMPANY, close.router, "Close"),
    # LAST: /reports/{report_id} must not answer for /reports/opening and the other steps.
    (COMPANY, report.router, "Report"),
]
