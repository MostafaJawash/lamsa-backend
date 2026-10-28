from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CHAR,
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, table_args
from app.db.mixins import CreatedAtMixin, IdMixin, TimestampMixin
from app.db.types import MONEY_ZERO_DEFAULT, Money, str_enum
from app.modules.products.enums import ProductType
from app.shared.enums import Currency

if TYPE_CHECKING:
    from app.modules.brands.models import Brand
    from app.modules.categories.models import Category


class Product(IdMixin, TimestampMixin, Base):
    """A sellable product: simple, variable (has variants) or bundle (gift box).

    price_from, in_stock and is_on_sale are denormalized and recalculated by the service
    layer whenever variants, prices or stock change.

    search_text is maintained by the service (app.modules.products.summary): the Arabic-normalized
    name, description, brand name, category and parent category names, text/select/multiselect
    attribute values and active variant option values. MySQL's FULLTEXT does not fold Arabic
    letter variants (أ/ا, ة/ه, ى/ي, tashkeel, tatweel), so both this column and the query are
    normalized by app.shared.utils.arabic.normalize_arabic.
    """

    __tablename__ = "products"
    __table_args__ = table_args(
        CheckConstraint(
            "sale_price IS NULL OR (price IS NOT NULL AND sale_price < price)",
            name="sale_price_lt_price",
        ),
        CheckConstraint("price IS NULL OR price >= 0", name="price_non_negative"),
        CheckConstraint("currency IN ('SYP', 'USD')", name="currency_supported"),
        CheckConstraint("stock IS NULL OR stock >= 0", name="stock_non_negative"),
        Index("ix_products_is_active_is_new", "is_active", "is_new"),
        Index("ix_products_is_active_is_on_sale", "is_active", "is_on_sale"),
        Index("ix_products_category_id_is_active", "category_id", "is_active"),
        Index("ix_products_brand_id_is_active", "brand_id", "is_active"),
        # SEARCH RULE: query this index only with
        #   MATCH(search_text) AGAINST(:q IN BOOLEAN MODE)
        # with :q built by app.shared.utils.search.build_boolean_query(). Natural-language mode
        # over-matches Arabic: the ngram parser (token size 2) splits words into bigrams, and
        # common ones such as "ال" match almost every product.
        Index(
            "ft_products_search_text",
            "search_text",
            mysql_prefix="FULLTEXT",
            mysql_with_parser="ngram",
        ),
    )

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(220), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    # FKs below are covered by the (category_id, is_active) and (brand_id, is_active) indexes.
    category_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("categories.id", ondelete="RESTRICT"))
    brand_id: Mapped[int | None] = mapped_column(  # gift boxes may have no brand
        BigInteger, ForeignKey("brands.id", ondelete="RESTRICT")
    )
    type: Mapped[ProductType] = mapped_column(str_enum(ProductType))
    sku: Mapped[str | None] = mapped_column(String(64), unique=True)  # simple and bundle
    currency: Mapped[Currency] = mapped_column(CHAR(3), default=Currency.syp)
    price: Mapped[Decimal | None] = mapped_column(Money)  # simple and bundle only
    sale_price: Mapped[Decimal | None] = mapped_column(Money)
    stock: Mapped[int | None] = mapped_column(Integer)  # simple only; bundle stock is derived
    attributes: Mapped[dict[str, Any]] = mapped_column(  # keyed by category_attributes.key
        JSON, server_default=text("(JSON_OBJECT())")
    )
    price_from: Mapped[Decimal] = mapped_column(Money, server_default=MONEY_ZERO_DEFAULT, index=True)
    in_stock: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    is_new: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    is_on_sale: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    # No server default (MySQL reflects it as _utf8mb4'', which alembic flags); the ORM always
    # writes it, and the service fills it via refresh_search_text().
    search_text: Mapped[str] = mapped_column(Text, default="")

    category: Mapped[Category] = relationship(back_populates="products", lazy="raise")
    brand: Mapped[Brand | None] = relationship(back_populates="products", lazy="raise")
    variants: Mapped[list[ProductVariant]] = relationship(
        back_populates="product",
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ProductVariant.sort_order",
    )
    images: Mapped[list[ProductImage]] = relationship(
        back_populates="product",
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ProductImage.sort_order",
    )
    bundle_items: Mapped[list[BundleItem]] = relationship(
        back_populates="bundle",
        foreign_keys="BundleItem.bundle_product_id",
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ProductVariant(IdMixin, TimestampMixin, Base):
    """One purchasable combination of a variable product's options, with its own price and stock.

    options_signature is built by app.modules.products.utils.build_options_signature.
    """

    __tablename__ = "product_variants"
    __table_args__ = table_args(
        UniqueConstraint(
            "product_id", "options_signature", name="uq_product_variants_product_id_options_signature"
        ),
        CheckConstraint("price >= 0", name="price_non_negative"),
        CheckConstraint("stock >= 0", name="stock_non_negative"),
        CheckConstraint("sale_price IS NULL OR sale_price < price", name="sale_price_lt_price"),
    )

    # Covered by the (product_id, options_signature) unique index.
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="CASCADE"))
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    # e.g. {"color": "أحمر كلاسيكي", "color_hex": "#B3202C"} or {"size": "50 ml"}
    options: Mapped[dict[str, Any]] = mapped_column(JSON)
    options_signature: Mapped[str] = mapped_column(String(255))
    price: Mapped[Decimal] = mapped_column(Money)
    sale_price: Mapped[Decimal | None] = mapped_column(Money)
    stock: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    product: Mapped[Product] = relationship(back_populates="variants", lazy="raise")


class ProductImage(IdMixin, CreatedAtMixin, Base):
    """A product image. image_key is the base key without size suffix, e.g. products/42/3f9c...e1."""

    __tablename__ = "product_images"
    __table_args__ = table_args(
        Index("ix_product_images_product_id_sort_order", "product_id", "sort_order"),
    )

    # Covered by the (product_id, sort_order) index.
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="CASCADE"))
    variant_id: Mapped[int | None] = mapped_column(  # image specific to one colour
        BigInteger, ForeignKey("product_variants.id", ondelete="SET NULL"), index=True
    )
    image_key: Mapped[str] = mapped_column(String(255))
    alt_text: Mapped[str | None] = mapped_column(String(255))
    width: Mapped[int] = mapped_column(Integer)  # of the largest stored size
    height: Mapped[int] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))  # 0 = main

    product: Mapped[Product] = relationship(back_populates="images", lazy="raise")
    variant: Mapped[ProductVariant | None] = relationship(lazy="raise")


class BundleItem(IdMixin, Base):
    """One line of a gift box (bundle product).

    TODO(service layer): reject bundle_product_id == product_id. MySQL forbids a CHECK on
    bundle_product_id because that column has an ON DELETE CASCADE foreign key.

    variant_key is VIRTUAL (not STORED): a stored generated column cannot be based on a
    column with a referential-action FK; VIRTUAL is used in cart_items too for consistency.
    """

    __tablename__ = "bundle_items"
    __table_args__ = table_args(
        UniqueConstraint(
            "bundle_product_id",
            "product_id",
            "variant_key",
            name="uq_bundle_items_bundle_product_id_product_id_variant_key",
        ),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )

    # Covered by the unique index above.
    bundle_product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("products.id", ondelete="CASCADE")
    )
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    variant_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("product_variants.id", ondelete="RESTRICT"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    variant_key: Mapped[int] = mapped_column(
        BigInteger, Computed("COALESCE(variant_id, 0)", persisted=False)
    )

    bundle: Mapped[Product] = relationship(
        back_populates="bundle_items", foreign_keys=[bundle_product_id], lazy="raise"
    )
    product: Mapped[Product] = relationship(foreign_keys=[product_id], lazy="raise")
    variant: Mapped[ProductVariant | None] = relationship(lazy="raise")
