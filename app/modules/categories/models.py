from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, table_args
from app.db.mixins import IdMixin, TimestampMixin
from app.db.types import NullableJSON, str_enum
from app.modules.categories.enums import AttributeType, VariantOptionType

if TYPE_CHECKING:
    from app.modules.products.models import Product


class Category(IdMixin, TimestampMixin, Base):
    """A product category, optionally nested under a parent.

    Inheritance rule (enforced by the service layer): a subcategory inherits all of its
    parent's attributes and variant options, in addition to its own.
    """

    __tablename__ = "categories"

    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("categories.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    image_key: Mapped[str | None] = mapped_column(String(255))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))

    parent: Mapped[Category | None] = relationship(
        back_populates="children", remote_side="Category.id", lazy="raise"
    )
    # parent_id is ON DELETE RESTRICT: let the database refuse, never null out children.
    children: Mapped[list[Category]] = relationship(
        back_populates="parent", lazy="raise", passive_deletes="all"
    )
    attributes: Mapped[list[CategoryAttribute]] = relationship(
        back_populates="category", lazy="raise", cascade="all, delete-orphan", passive_deletes=True
    )
    variant_options: Mapped[list[CategoryVariantOption]] = relationship(
        back_populates="category", lazy="raise", cascade="all, delete-orphan", passive_deletes=True
    )
    products: Mapped[list[Product]] = relationship(
        back_populates="category", lazy="raise", passive_deletes="all"
    )


class CategoryAttribute(IdMixin, Base):
    """Describes the product as a whole (e.g. skin_type); the customer does not choose it.

    Values live in products.attributes keyed by `key`.
    """

    __tablename__ = "category_attributes"
    __table_args__ = table_args(
        UniqueConstraint("category_id", "key", name="uq_category_attributes_category_id_key"),
    )

    category_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("categories.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(50))  # e.g. skin_type, cocoa_percent
    label: Mapped[str] = mapped_column(String(100))  # Arabic label shown in the UI
    type: Mapped[AttributeType] = mapped_column(str_enum(AttributeType))
    options: Mapped[list[str] | None] = mapped_column(NullableJSON)  # allowed values for select/multiselect
    unit: Mapped[str | None] = mapped_column(String(20))  # e.g. "%", "g"
    is_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    is_filterable: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    category: Mapped[Category] = relationship(back_populates="attributes", lazy="raise")


class CategoryVariantOption(IdMixin, Base):
    """An axis the customer chooses (color, size, ...); each variant has its own price and stock."""

    __tablename__ = "category_variant_options"
    __table_args__ = table_args(
        UniqueConstraint("category_id", "key", name="uq_category_variant_options_category_id_key"),
    )

    category_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("categories.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(50))  # color, size, weight
    label: Mapped[str] = mapped_column(String(100))
    type: Mapped[VariantOptionType] = mapped_column(str_enum(VariantOptionType))
    options: Mapped[list[str] | None] = mapped_column(NullableJSON)  # predefined values, e.g. ["30 ml", "50 ml"]
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    category: Mapped[Category] = relationship(back_populates="variant_options", lazy="raise")
