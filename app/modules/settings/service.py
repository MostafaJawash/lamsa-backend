"""Store settings with typed defaults. The ONLY place settings are read.

A key missing in store_settings (or holding an invalid value) falls back to its default.
"""

import json
from dataclasses import dataclass, fields
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


# ------------------------------------------------------------------- notifications


@dataclass(frozen=True)
class NotificationSettings:
    timezone: str = "Europe/Berlin"
    quiet_hours_start: time = time(21, 0)
    quiet_hours_end: time = time(9, 0)
    favorite_alert_cooldown_days: int = 7
    price_drop_min_percent: int = 5
    cart_reminder_after_hours: int = 24
    notifications_retention_days: int = 180

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


NOTIFICATION_DEFAULTS = NotificationSettings()
NOTIFICATION_KEYS: tuple[str, ...] = tuple(f.name for f in fields(NotificationSettings))
INT_MINIMUMS: dict[str, int] = {
    "favorite_alert_cooldown_days": 0, "price_drop_min_percent": 0, "cart_reminder_after_hours": 1,
    "notifications_retention_days": 1,
}


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _parse_time(value: Any, default: time) -> time:
    try:
        hours, minutes = str(_json(value)).split(":")
        return time(int(hours), int(minutes))
    except (ValueError, TypeError):
        return default


def notification_settings_from(values: dict[str, Any]) -> NotificationSettings:
    d = NOTIFICATION_DEFAULTS
    zone = _json(values.get("timezone"))
    timezone = d.timezone
    if isinstance(zone, str):
        try:
            ZoneInfo(zone)
            timezone = zone
        except (ZoneInfoNotFoundError, ValueError):
            pass
    ints: dict[str, int] = {}
    for key, minimum in INT_MINIMUMS.items():
        raw = _json(values.get(key))
        ints[key] = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= minimum else getattr(d, key)
    return NotificationSettings(
        timezone=timezone,
        quiet_hours_start=_parse_time(values.get("quiet_hours_start"), d.quiet_hours_start),
        quiet_hours_end=_parse_time(values.get("quiet_hours_end"), d.quiet_hours_end),
        **ints,
    )


async def load_notification_settings(session: AsyncSession) -> NotificationSettings:
    rows = await session.execute(
        select(StoreSetting.key, StoreSetting.value).where(StoreSetting.key.in_(NOTIFICATION_KEYS))
    )
    return notification_settings_from({key: value for key, value in rows.all()})
