from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import IdMixin, TimestampMixin

if TYPE_CHECKING:
    from app.modules.products.models import Product


class Brand(IdMixin, TimestampMixin, Base):
    __tablename__ = "brands"

    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    logo_key: Mapped[str | None] = mapped_column(String(255))
    is_own_brand: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    # products.brand_id is ON DELETE RESTRICT: let the database refuse.
    products: Mapped[list[Product]] = relationship(
        back_populates="brand", lazy="raise", passive_deletes="all"
    )
