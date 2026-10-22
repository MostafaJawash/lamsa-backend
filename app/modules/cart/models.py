from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, Computed, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, table_args
from app.db.mixins import IdMixin, TimestampMixin

if TYPE_CHECKING:
    from app.modules.products.models import Product, ProductVariant
    from app.modules.users.models import User


class CartItem(IdMixin, TimestampMixin, Base):
    """A line in a user's cart. No price is stored: it is always computed live.

    MySQL treats NULLs as distinct in UNIQUE indexes, so uniqueness uses variant_key
    (variant_id or 0). It is VIRTUAL because a STORED generated column cannot be based on
    variant_id, which has an ON DELETE CASCADE foreign key.
    """

    __tablename__ = "cart_items"
    __table_args__ = table_args(
        UniqueConstraint(
            "user_id", "product_id", "variant_key", name="uq_cart_items_user_id_product_id_variant_key"
        ),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )

    # Covered by the unique index above.
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    variant_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("product_variants.id", ondelete="CASCADE"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer)
    variant_key: Mapped[int] = mapped_column(
        BigInteger, Computed("COALESCE(variant_id, 0)", persisted=False)
    )

    user: Mapped[User] = relationship(back_populates="cart_items", lazy="raise")
    product: Mapped[Product] = relationship(lazy="raise")
    variant: Mapped[ProductVariant | None] = relationship(lazy="raise")
