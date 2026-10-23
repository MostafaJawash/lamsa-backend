"""Money in the API: Decimal with exactly 2 decimals, serialized as a JSON string ("8.95")."""

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import AfterValidator, PlainSerializer, WithJsonSchema

MAX_MONEY = Decimal("99999999.99")
CENT = Decimal("0.01")


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


_MONEY_JSON = WithJsonSchema({"type": "string", "pattern": r"^\d{1,8}\.\d{2}$", "examples": ["8.95"]})

# Request field: accepts "8.95" or 8.95; validated and quantized to 2 decimals.
MoneyIn = Annotated[Decimal, AfterValidator(_validate_money)]
# Response field: always a string with 2 decimals.
MoneyOut = Annotated[Decimal, PlainSerializer(format_money, return_type=str, when_used="json"), _MONEY_JSON]


def discount_percent(regular: Decimal, current: Decimal) -> int | None:
    """Rounded percent saved, or None when there is no discount."""
    if regular <= 0 or current >= regular:
        return None
    return int(((regular - current) / regular * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
