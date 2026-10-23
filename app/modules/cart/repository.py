"""Cart queries. Each function is ONE SQL statement (see the query-budget tests)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.brands.models import Brand
from app.modules.cart.models import CartItem
from app.modules.delivery.models import DeliveryMethod
from app.modules.products.enums import ProductType
from app.modules.products.models import Product, ProductImage, ProductVariant
from app.modules.products.presenters import LoadedImage
from app.modules.products.visibility import visible_product_condition
from app.modules.settings.service import StoreLimits, limit_columns, limits_from_row
from app.modules.users.models import User


async def lock_user(session: AsyncSession, user_id: int) -> StoreLimits:
    """Per-user lock for cart mutations (SELECT ... FOR UPDATE on the user row), plus the store
    limits as scalar subqueries in the same statement. Subqueries are not locked."""
    row = (
        await session.execute(select(User.id, *limit_columns()).where(User.id == user_id).with_for_update())
    ).one()
    return limits_from_row(row)


@dataclass(frozen=True)
class LineRef:
    id: int
    product_id: int
    variant_id: int | None
    quantity: int
    product_type: ProductType


async def lock_user_with_lines(session: AsyncSession, user_id: int) -> tuple[StoreLimits, list[LineRef]]:
    """ONE statement: lock the user row, read the store limits (scalar subqueries) and the user's
    line references (LEFT JOIN).

    This must be a full locking read (FOR UPDATE, which also locks the user's cart rows): under
    REPEATABLE READ a non-locking read of cart_items would use the snapshot taken by the
    request's first query (authentication), i.e. from BEFORE a parallel request committed."""
    rows = (
        await session.execute(
            select(
                User.id.label("user_id"), *limit_columns(), CartItem.id.label("line_id"), CartItem.product_id,
                CartItem.variant_id, CartItem.quantity, Product.type,
            )
            .select_from(User)
            .outerjoin(CartItem, CartItem.user_id == User.id)
            .outerjoin(Product, Product.id == CartItem.product_id)
            .where(User.id == user_id)
            .order_by(CartItem.created_at, CartItem.id)
            .with_for_update()
        )
    ).all()
    refs = [LineRef(r.line_id, r.product_id, r.variant_id, r.quantity, r.type) for r in rows if r.line_id is not None]
    return limits_from_row(rows[0]), refs


@dataclass
class LineRow:
    id: int
    quantity: int
    product_id: int
    slug: str
    name: str
    type: ProductType
    price: Decimal | None
    sale_price: Decimal | None
    stock: int | None
    visible: bool
    brand_name: str | None
    brand_slug: str | None
    variant_id: int | None
    variant_options: dict[str, Any] | None
    variant_price: Decimal | None
    variant_sale_price: Decimal | None
    variant_stock: int | None
    variant_active: bool | None
    product_sku: str | None = None
    variant_sku: str | None = None


async def load_lines(
    session: AsyncSession, user_id: int, *, lock: bool = False, lock_products: bool = False
) -> list[LineRow]:
    """All of the user's lines with product, brand, variant and visibility, oldest first.

    lock=True (inside a cart mutation): FOR UPDATE OF cart_items, a locking read that sees the
    latest committed lines; product data stays a plain read so the cart never blocks admin edits.
    lock_products=True (checkout, after the product/variant rows were locked in id order): also
    FOR UPDATE OF products and variants, so prices and stock are the current values, not the
    request's snapshot. Those rows are already locked, so this adds no new waiting."""
    stmt = (
        select(
            CartItem.id, CartItem.quantity, Product.id.label("product_id"), Product.slug, Product.name, Product.type,
            Product.price, Product.sale_price, Product.stock, visible_product_condition().label("visible"),
            Brand.name.label("brand_name"), Brand.slug.label("brand_slug"), ProductVariant.id.label("variant_id"),
            ProductVariant.options, ProductVariant.price.label("variant_price"),
            ProductVariant.sale_price.label("variant_sale_price"), ProductVariant.stock.label("variant_stock"),
            ProductVariant.is_active.label("variant_active"), Product.sku.label("product_sku"),
            ProductVariant.sku.label("variant_sku"),
        )
        .join(Product, Product.id == CartItem.product_id)
        .outerjoin(Brand, Brand.id == Product.brand_id)
        .outerjoin(ProductVariant, ProductVariant.id == CartItem.variant_id)
        .where(CartItem.user_id == user_id)
        .order_by(CartItem.created_at, CartItem.id)
    )
    if lock_products:
        stmt = stmt.with_for_update(of=[CartItem, Product, ProductVariant])
    elif lock:
        stmt = stmt.with_for_update(of=CartItem)
    rows = await session.execute(stmt)
    return [
        LineRow(
            id=r.id, quantity=r.quantity, product_id=r.product_id, slug=r.slug, name=r.name, type=r.type,
            price=r.price, sale_price=r.sale_price, stock=r.stock, visible=bool(r.visible), brand_name=r.brand_name,
            brand_slug=r.brand_slug, variant_id=r.variant_id, variant_options=r.options,
            variant_price=r.variant_price, variant_sale_price=r.variant_sale_price, variant_stock=r.variant_stock,
            variant_active=r.variant_active, product_sku=r.product_sku, variant_sku=r.variant_sku,
        )
        for r in rows
    ]


@dataclass(frozen=True)
class TargetProduct:
    id: int
    type: ProductType
    stock: int | None
    visible: bool


async def target_products(session: AsyncSession, product_ids: Sequence[int]) -> dict[int, TargetProduct]:
    if not product_ids:
        return {}
    rows = await session.execute(
        select(Product.id, Product.type, Product.stock, visible_product_condition().label("visible")).where(
            Product.id.in_(product_ids)
        )
    )
    return {r.id: TargetProduct(r.id, r.type, r.stock, bool(r.visible)) for r in rows}


async def images_for(session: AsyncSession, product_ids: Sequence[int]) -> dict[int, list[LoadedImage]]:
    if not product_ids:
        return {}
    rows = await session.execute(
        select(
            ProductImage.product_id, ProductImage.id, ProductImage.variant_id, ProductImage.image_key,
            ProductImage.alt_text, ProductImage.width, ProductImage.height, ProductImage.sort_order,
        ).where(ProductImage.product_id.in_(product_ids))
    )
    result: dict[int, list[LoadedImage]] = {}
    for r in rows:
        result.setdefault(r.product_id, []).append(
            LoadedImage(r.id, r.variant_id, r.image_key, r.alt_text, r.width, r.height, r.sort_order)
        )
    return result


async def active_delivery_methods(session: AsyncSession) -> tuple[list[DeliveryMethod], StoreLimits | None]:
    """Active delivery methods, with the store limits riding along as scalar subqueries (so a
    cart read needs no separate settings query). Limits are None only if no method is active."""
    rows = (
        await session.execute(
            select(DeliveryMethod, *limit_columns())
            .where(DeliveryMethod.is_active.is_(True))
            .order_by(DeliveryMethod.sort_order, DeliveryMethod.id)
        )
    ).all()
    return [r[0] for r in rows], (limits_from_row(rows[0]) if rows else None)


# ------------------------------------------------------------------------------- writes
# created_at/updated_at are set explicitly and updates use core statements, so the ORM's
# eager_defaults never adds a re-select.


async def insert_lines(session: AsyncSession, user_id: int, rows: Sequence[tuple[int, int | None, int]], now: datetime) -> None:
    """Insert (product_id, variant_id, quantity) rows in ONE statement."""
    if rows:
        await session.execute(
            insert(CartItem),
            [
                {"user_id": user_id, "product_id": p, "variant_id": v, "quantity": q, "created_at": now, "updated_at": now}
                for p, v, q in rows
            ],
        )


async def set_quantities(session: AsyncSession, changes: Sequence[tuple[int, int]], now: datetime) -> None:
    """(line_id, quantity) updates in ONE executemany statement."""
    if changes:
        await session.execute(
            update(CartItem),
            [{"id": line_id, "quantity": quantity, "updated_at": now} for line_id, quantity in changes],
        )


async def set_line(session: AsyncSession, line_id: int, *, quantity: int, variant_id: int | None, now: datetime) -> None:
    await session.execute(
        update(CartItem)
        .where(CartItem.id == line_id)
        .values(quantity=quantity, variant_id=variant_id, updated_at=now)
        .execution_options(synchronize_session=False)
    )


async def delete_lines(session: AsyncSession, user_id: int, line_ids: Sequence[int] | None = None) -> None:
    stmt = delete(CartItem).where(CartItem.user_id == user_id)
    if line_ids is not None:
        if not line_ids:
            return
        stmt = stmt.where(CartItem.id.in_(line_ids))
    await session.execute(stmt)
