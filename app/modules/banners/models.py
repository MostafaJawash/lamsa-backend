from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, table_args
from app.db.mixins import IdMixin, TimestampMixin
from app.db.types import str_enum
from app.shared.enums import LinkType


class Banner(IdMixin, TimestampMixin, Base):
    __tablename__ = "banners"
    __table_args__ = table_args(
        CheckConstraint(
            "ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at",
            name="ends_after_starts",
        ),
    )

    title: Mapped[str] = mapped_column(String(150))
    subtitle: Mapped[str | None] = mapped_column(String(255))
    image_key: Mapped[str] = mapped_column(String(255))
    link_type: Mapped[LinkType] = mapped_column(
        str_enum(LinkType), default=LinkType.none, server_default=LinkType.none.value
    )
    link_value: Mapped[str | None] = mapped_column(String(255))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime)
