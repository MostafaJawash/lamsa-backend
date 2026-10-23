from enum import StrEnum

from sqlalchemy import JSON, Enum, Numeric, text

# Money is always DECIMAL(10,2); never Float.
Money = Numeric(10, 2)

# Use for every money column defaulting to zero: MySQL reflects DECIMAL(10,2) defaults as '0.00'.
MONEY_ZERO_DEFAULT = text("0.00")

# Use for every nullable JSON column: Python None is stored as SQL NULL, not the JSON value null.
NullableJSON = JSON(none_as_null=True)


def str_enum(enum_cls: type[StrEnum], length: int = 30) -> Enum:
    """Store a StrEnum as VARCHAR(length) holding its lowercase values (no native MySQL ENUM)."""
    return Enum(
        enum_cls,
        native_enum=False,
        length=length,
        validate_strings=True,
        values_callable=lambda members: [m.value for m in members],
    )
