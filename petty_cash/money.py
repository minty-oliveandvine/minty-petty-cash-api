"""Money as Flask's API writes it: ``NUMERIC(14,2)`` read as a float rounded to cents (Flask's
``Money`` column type and ``blueprints/shared/column_types.cents``). Internally the services
work in ``Decimal``; these are the two edges."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
#: What an amount field accepts (Flask's currency.js and ``MAX_AMOUNT``).
MAX_AMOUNT = Decimal("99999999999.99")


def cents(value) -> Decimal:
    """A Decimal rounded half-up to the cent; None is 0."""
    if value is None:
        return Decimal("0.00")
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def out(value) -> float:
    """The JSON number Flask answers with (``155.0``, never ``"155.00"``)."""
    return float(cents(value))


def out_or_none(value) -> float | None:
    return None if value is None else out(value)
