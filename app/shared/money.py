"""Money in the API: Decimal with exactly 2 decimals, serialized as a JSON string ("8.95")."""

from decimal import ROUND_HALF_UP, Decimal
from collections.abc import Iterable, Mapping
from typing import Annotated

from pydantic import AfterValidator, PlainSerializer, WithJsonSchema

from app.shared.enums import Currency

MAX_MONEY = Decimal("9999999999999.99")
CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def _validate_money(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("must be a number")
    exponent = value.normalize().as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise ValueError("at most 2 decimal places")
    if value < 0 or value > MAX_MONEY:
        raise ValueError(f"must be between 0 and {MAX_MONEY}")
    return value.quantize(CENT)


def format_money(value: Decimal) -> str:
    return f"{value.quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def sum_by_currency(lines: Iterable[tuple[Currency, Decimal]]) -> dict[Currency, Decimal]:
    """Sum only within the same currency. This is the single aggregation rule for carts/orders."""
    totals: dict[Currency, Decimal] = {}
    for currency, amount in lines:
        totals[currency] = totals.get(currency, ZERO) + amount
    return totals


def totals_with_fee(
    subtotals: Mapping[Currency, Decimal], fee_currency: Currency, fee: Decimal
) -> dict[Currency, Decimal]:
    totals = dict(subtotals)
    totals[fee_currency] = totals.get(fee_currency, ZERO) + fee
    return totals


def ordered_currencies(values: Mapping[Currency, Decimal]) -> list[Currency]:
    return [currency for currency in (Currency.syp, Currency.usd) if currency in values]


_MONEY_JSON = WithJsonSchema({"type": "string", "pattern": r"^\d{1,13}\.\d{2}$", "examples": ["15000.00"]})

# Request field: accepts "8.95" or 8.95; validated and quantized to 2 decimals.
MoneyIn = Annotated[Decimal, AfterValidator(_validate_money)]
# Response field: always a string with 2 decimals.
MoneyOut = Annotated[Decimal, PlainSerializer(format_money, return_type=str, when_used="json"), _MONEY_JSON]


def discount_percent(regular: Decimal, current: Decimal) -> int | None:
    """Rounded percent saved, or None when there is no discount."""
    if regular <= 0 or current >= regular:
        return None
    return int(((regular - current) / regular * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
