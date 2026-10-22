from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import CreatedAtMixin

if TYPE_CHECKING:
    from app.modules.products.models import Product
    from app.modules.users.models import User


class Favorite(CreatedAtMixin, Base):
    __tablename__ = "favorites"

    # user_id is covered by the primary key (user_id, product_id).
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("products.id", ondelete="CASCADE"), primary_key=True, index=True
    )

    user: Mapped[User] = relationship(back_populates="favorites", lazy="raise")
    product: Mapped[Product] = relationship(lazy="raise")
