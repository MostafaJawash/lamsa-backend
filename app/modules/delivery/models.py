from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Boolean, CHAR, CheckConstraint, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, table_args
from app.db.mixins import IdMixin, TimestampMixin
from app.db.types import MONEY_ZERO_DEFAULT, Money, NullableJSON
from app.shared.enums import Currency


class DeliveryMethod(IdMixin, TimestampMixin, Base):
    __tablename__ = "delivery_methods"
    __table_args__ = table_args(
        CheckConstraint("fee >= 0", name="fee_non_negative"),
        CheckConstraint("currency IN ('SYP', 'USD')", name="currency_supported"),
    )

    code: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(String(255))
    fee: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), server_default=MONEY_ZERO_DEFAULT)
    free_from: Mapped[Decimal | None] = mapped_column(Money)  # free shipping threshold
    currency: Mapped[Currency] = mapped_column(CHAR(3), default=Currency.syp)
    # NULL means unrestricted; live Syrian methods use explicit governorate-code lists.
    allowed_governorates: Mapped[list[str] | None] = mapped_column(NullableJSON)
    fee_paid_to_carrier: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0")
    )
    requires_address: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
