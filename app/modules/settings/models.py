from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, FetchedValue, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import CURRENT_TIMESTAMP_ON_UPDATE


class StoreSetting(Base):
    """A key/value store setting; value is any JSON (string, number, null, object...)."""

    __tablename__ = "store_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=CURRENT_TIMESTAMP_ON_UPDATE, server_onupdate=FetchedValue()
    )
