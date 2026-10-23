"""Favorites queries. Rows for products that are no longer visible stay in the table (they come
back when the product is re-published) but are never listed."""

from collections.abc import Sequence

from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock
from app.modules.favorites.models import Favorite
from app.modules.products import repository as product_repo
from app.modules.products.models import Product
from app.modules.products.presenters import CardRow
from app.modules.products.visibility import visible_product_condition


async def favorite_cards(
    session: AsyncSession, user_id: int, offset: int, limit: int
) -> tuple[list[CardRow], int | None]:
    """Visible favorites, newest favorite first, with the total via COUNT(*) OVER()."""
    rows = (
        await session.execute(
            product_repo.card_select(func.count().over().label("total"))
            .join(Favorite, and_(Favorite.product_id == Product.id, Favorite.user_id == user_id))
            .where(visible_product_condition())
            .order_by(Favorite.created_at.desc(), Product.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return [product_repo.card_row(r) for r in rows], (int(rows[0].total) if rows else None)


async def count_visible(session: AsyncSession, user_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count(Favorite.product_id))
            .join(Product, Product.id == Favorite.product_id)
            .where(Favorite.user_id == user_id, visible_product_condition())
        )
        or 0
    )


async def visible_favorite_ids(session: AsyncSession, user_id: int) -> list[int]:
    rows = await session.scalars(
        select(Favorite.product_id)
        .join(Product, Product.id == Favorite.product_id)
        .where(Favorite.user_id == user_id, visible_product_condition())
        .order_by(Favorite.created_at.desc(), Favorite.product_id.desc())
    )
    return list(rows.all())


async def favorite_ids_among(session: AsyncSession, user_id: int, product_ids: Sequence[int]) -> set[int]:
    """Which of these products the user has favorited (for is_favorite): ONE query."""
    if not product_ids:
        return set()
    rows = await session.scalars(
        select(Favorite.product_id).where(Favorite.user_id == user_id, Favorite.product_id.in_(product_ids))
    )
    return set(rows.all())


async def all_favorite_ids(session: AsyncSession, user_id: int) -> set[int]:
    """Every favorite row of the user (visible or not); its size is what the limit counts."""
    return set((await session.scalars(select(Favorite.product_id).where(Favorite.user_id == user_id))).all())


async def visible_product_ids(session: AsyncSession, product_ids: Sequence[int]) -> set[int]:
    if not product_ids:
        return set()
    rows = await session.scalars(select(Product.id).where(Product.id.in_(product_ids), visible_product_condition()))
    return set(rows.all())


async def add_favorites(session: AsyncSession, user_id: int, product_ids: Sequence[int]) -> None:
    """INSERT ... ON DUPLICATE KEY UPDATE: idempotent and race-free, keeps the original date."""
    if not product_ids:
        return
    now = clock.utcnow()
    stmt = mysql_insert(Favorite).values([{"user_id": user_id, "product_id": p, "created_at": now} for p in product_ids])
    await session.execute(stmt.on_duplicate_key_update(product_id=stmt.inserted.product_id))


async def remove_favorite(session: AsyncSession, user_id: int, product_id: int) -> None:
    await session.execute(delete(Favorite).where(Favorite.user_id == user_id, Favorite.product_id == product_id))
