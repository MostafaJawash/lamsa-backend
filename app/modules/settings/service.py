"""Store settings with typed defaults. The ONLY place settings are read.

A key missing in store_settings (or holding an invalid value) falls back to its default.
"""

import json
from dataclasses import dataclass, fields
from typing import Any

from sqlalchemy import ScalarSelect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.settings.models import StoreSetting


@dataclass(frozen=True)
class StoreLimits:
    low_stock_threshold: int = 3
    cart_max_quantity_per_line: int = 10
    cart_max_lines: int = 50
    favorites_max: int = 200


DEFAULTS = StoreLimits()
LIMIT_KEYS: tuple[str, ...] = tuple(f.name for f in fields(StoreLimits))
# Smallest allowed value per key (a threshold may be 0, limits at least 1).
MINIMUMS: dict[str, int] = {"low_stock_threshold": 0, "cart_max_quantity_per_line": 1, "cart_max_lines": 1, "favorites_max": 1}


def parse_int_setting(value: Any, key: str) -> int:
    """JSON value -> int, or the key's default when missing/invalid."""
    default: int = getattr(DEFAULTS, key)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return default
    if isinstance(value, bool) or not isinstance(value, int) or value < MINIMUMS[key]:
        return default
    return value


def limits_from_values(values: dict[str, Any]) -> StoreLimits:
    return StoreLimits(**{key: parse_int_setting(values.get(key), key) for key in LIMIT_KEYS})


async def load_limits(session: AsyncSession) -> StoreLimits:
    rows = await session.execute(select(StoreSetting.key, StoreSetting.value).where(StoreSetting.key.in_(LIMIT_KEYS)))
    return limits_from_values({key: value for key, value in rows.all()})


def setting_subquery(key: str) -> ScalarSelect[Any]:
    """Scalar subquery so a setting rides along in another query (no extra statement)."""
    return select(StoreSetting.value).where(StoreSetting.key == key).scalar_subquery()


def limit_columns() -> list[Any]:
    return [setting_subquery(key).label(f"setting_{key}") for key in LIMIT_KEYS]


def limits_from_row(row: Any) -> StoreLimits:
    return limits_from_values({key: getattr(row, f"setting_{key}") for key in LIMIT_KEYS})


async def get_low_stock_threshold(session: AsyncSession) -> int:
    return (await load_limits(session)).low_stock_threshold
