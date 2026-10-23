"""Schema rules every model must follow. No database needed."""

from sqlalchemy import JSON, Numeric

import app.db.models  # noqa: F401  (registers every model)
from app.db.base import TABLE_OPTIONS, Base
from app.db.types import MONEY_ZERO_DEFAULT


def test_every_table_has_innodb_utf8mb4_options() -> None:
    for table in Base.metadata.tables.values():
        for option, value in TABLE_OPTIONS.items():
            assert table.kwargs.get(option) == value, f"{table.name} is missing {option}={value}"


def test_money_zero_defaults_use_shared_constant() -> None:
    for table in Base.metadata.tables.values():
        for column in table.columns:
            default = column.server_default
            if isinstance(column.type, Numeric) and default is not None:
                assert default.arg is MONEY_ZERO_DEFAULT, (
                    f"{table.name}.{column.name}: use MONEY_ZERO_DEFAULT "
                    "(MySQL reflects DECIMAL(10,2) defaults as '0.00')"
                )


def test_nullable_json_columns_store_sql_null() -> None:
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, JSON) and column.nullable:
                assert column.type.none_as_null, f"{table.name}.{column.name}: use NullableJSON"


def test_expected_tables() -> None:
    assert len(Base.metadata.tables) == 24
