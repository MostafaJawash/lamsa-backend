from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, table_args
from app.db.mixins import CreatedAtMixin, IdMixin, TimestampMixin
from app.db.types import MONEY_ZERO_DEFAULT, Money, NullableJSON, str_enum
from app.modules.orders.enums import OrderStatus, PaymentMethod, PaymentStatus

if TYPE_CHECKING:
    from app.modules.delivery.models import DeliveryMethod
    from app.modules.users.models import User


class Order(IdMixin, TimestampMixin, Base):
    """A placed order. id is the visible order number (AUTO_INCREMENT starts at 1001).

    Customer, address and delivery method are snapshots taken at checkout.
    """

    __tablename__ = "orders"
    __table_args__ = table_args(
        Index("ix_orders_user_id_created_at", "user_id", "created_at"),
        Index("ix_orders_status_created_at", "status", "created_at"),
        # One order per (user, Idempotency-Key); MySQL allows many NULL keys.
        UniqueConstraint("user_id", "idempotency_key", name="uq_orders_user_id_idempotency_key"),
        CheckConstraint("subtotal >= 0", name="subtotal_non_negative"),
        CheckConstraint("discount >= 0", name="discount_non_negative"),
        CheckConstraint("shipping_fee >= 0", name="shipping_fee_non_negative"),
        CheckConstraint("total >= 0", name="total_non_negative"),
        mysql_auto_increment="1001",
    )

    # Covered by the (user_id, created_at) index.
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    customer_name: Mapped[str] = mapped_column(String(100))
    customer_phone: Mapped[str] = mapped_column(String(20))
    # {label, city, street, building, notes}; NULL for pickup.
    shipping_address: Mapped[dict[str, Any] | None] = mapped_column(NullableJSON)
    delivery_method_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("delivery_methods.id", ondelete="SET NULL"), index=True
    )
    delivery_method_code: Mapped[str] = mapped_column(String(30))
    delivery_method_name: Mapped[str] = mapped_column(String(100))
    payment_method: Mapped[PaymentMethod] = mapped_column(
        str_enum(PaymentMethod), default=PaymentMethod.cod, server_default=PaymentMethod.cod.value
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        str_enum(PaymentStatus), default=PaymentStatus.pending, server_default=PaymentStatus.pending.value
    )
    status: Mapped[OrderStatus] = mapped_column(
        str_enum(OrderStatus), default=OrderStatus.pending, server_default=OrderStatus.pending.value
    )
    subtotal: Mapped[Decimal] = mapped_column(Money)
    discount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), server_default=MONEY_ZERO_DEFAULT)
    shipping_fee: Mapped[Decimal] = mapped_column(Money)
    total: Mapped[Decimal] = mapped_column(Money)
    customer_notes: Mapped[str | None] = mapped_column(String(500))
    gift_message: Mapped[str | None] = mapped_column(String(500))
    cancel_reason: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str | None] = mapped_column(String(64))  # from the Idempotency-Key header

    user: Mapped[User] = relationship(back_populates="orders", lazy="raise")
    delivery_method: Mapped[DeliveryMethod | None] = relationship(lazy="raise")
    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", lazy="raise", cascade="all, delete-orphan", passive_deletes=True
    )
    status_history: Mapped[list[OrderStatusHistory]] = relationship(
        back_populates="order",
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="OrderStatusHistory.created_at",
    )


class OrderItem(IdMixin, Base):
    """A snapshot of one ordered line; product/variant links survive only while they exist."""

    __tablename__ = "order_items"
    __table_args__ = table_args(
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )

    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("products.id", ondelete="SET NULL"), index=True
    )
    variant_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("product_variants.id", ondelete="SET NULL"), index=True
    )
    product_name: Mapped[str] = mapped_column(String(200))
    variant_label: Mapped[str | None] = mapped_column(String(200))  # e.g. "أحمر كلاسيكي"
    options: Mapped[dict[str, Any] | None] = mapped_column(NullableJSON)
    sku: Mapped[str | None] = mapped_column(String(64))
    image_key: Mapped[str | None] = mapped_column(String(255))
    unit_price: Mapped[Decimal] = mapped_column(Money)
    quantity: Mapped[int] = mapped_column(Integer)
    line_total: Mapped[Decimal] = mapped_column(Money)
    bundle_contents: Mapped[list[dict[str, Any]] | None] = mapped_column(NullableJSON)  # gift box items

    order: Mapped[Order] = relationship(back_populates="items", lazy="raise")


class OrderStatusHistory(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "order_status_history"

    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[OrderStatus | None] = mapped_column(str_enum(OrderStatus))
    to_status: Mapped[OrderStatus] = mapped_column(str_enum(OrderStatus))
    changed_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    note: Mapped[str | None] = mapped_column(String(255))

    order: Mapped[Order] = relationship(back_populates="status_history", lazy="raise")
    changed_by: Mapped[User | None] = relationship(lazy="raise")
