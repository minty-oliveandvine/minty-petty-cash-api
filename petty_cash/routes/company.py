"""``GET /companies/{id}``: the company the app is in and who is using it - what the app shell
needs before any page (its name, the person's role there, the currency every amount is in).
Flask's ``petty_cash_api/routes/company.py``."""

from __future__ import annotations

from minty_db.models import CurrencyInfo
from ninja import Router

from petty_cash.people import person_payload

router = Router()


def company_payload(person, company) -> dict:
    currency = CurrencyInfo.objects.filter(id=company.currency_id).first() if company.currency_id else None
    return {
        "company": {
            "id": company.id,
            "name": company.name,
            "role": company.role,
            "role_label": company.role_label,
            "currency": (
                {"code": currency.currency_code.strip(), "decimal_places": currency.decimal_places}
                if currency is not None
                else None
            ),
        },
        "person": person_payload(person),
    }


@router.get("")
def company_context(request):
    return company_payload(request.auth_user, request.company)
