"""Settings the admin API may read and change: a small registry, one entry per key.

To expose another setting: add an entry here (its default and the parser that turns the stored
JSON into the typed value) and the same key, with its type, to AdminSettingsOut and
AdminSettingsUpdate (app.modules.settings.schemas); a test keeps the three in sync.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.settings.models import StoreSetting
from app.modules.settings.schemas import AdminSettingsOut, AdminSettingsUpdate
from app.modules.settings.service import (
    CATALOG_HIDE_OUT_OF_STOCK,
    HIDE_OUT_OF_STOCK_DEFAULT,
    parse_bool_setting,
)
from app.shared.events import commit_and_publish


@dataclass(frozen=True)
class AdminSetting:
    default: Any
    parse: Callable[[Any], Any]  # stored JSON -> typed value (the default when missing/invalid)


ADMIN_SETTINGS: dict[str, AdminSetting] = {
    CATALOG_HIDE_OUT_OF_STOCK: AdminSetting(
        default=HIDE_OUT_OF_STOCK_DEFAULT, parse=lambda v: parse_bool_setting(v, HIDE_OUT_OF_STOCK_DEFAULT)
    ),
}


class AdminSettingsService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self) -> AdminSettingsOut:
        rows = await self.session.execute(
            select(StoreSetting.key, StoreSetting.value).where(StoreSetting.key.in_(ADMIN_SETTINGS))
        )
        stored = {key: value for key, value in rows.all()}
        return AdminSettingsOut(**{key: spec.parse(stored.get(key)) for key, spec in ADMIN_SETTINGS.items()})

    async def update(self, data: AdminSettingsUpdate) -> AdminSettingsOut:
        """Writes the keys sent. The router's catalog_write dependency bumps the catalog version
        after the commit, so cached lists are recomputed with the new values."""
        for key in sorted(data.model_fields_set):
            setting = await self.session.get(StoreSetting, key)
            value = getattr(data, key)
            if setting is None:
                self.session.add(StoreSetting(key=key, value=value))
            else:
                setting.value = value
        await commit_and_publish(self.session)
        return await self.get()
