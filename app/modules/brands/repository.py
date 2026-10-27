from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.brands.models import Brand
from app.modules.products.models import Product
from app.modules.products.visibility import visible_product_condition


async def get_brand(session: AsyncSession, brand_id: int, *, for_update: bool = False) -> Brand | None:
    stmt = select(Brand).where(Brand.id == brand_id)
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def visible_brands_with_counts(
    session: AsyncSession, category_ids: Sequence[int] | None, *, in_stock_only: bool = False
) -> list[tuple[Brand, int]]:
    """Active brands with their visible product counts (in_stock_only: in-stock ones only, the
    catalog_hide_out_of_stock setting). With category_ids, only brands that have such products in
    those categories, counted there."""
    counts = (
        select(Product.brand_id.label("brand_id"), func.count(Product.id).label("n"))
        .where(visible_product_condition(), Product.brand_id.is_not(None))
        .group_by(Product.brand_id)
    )
    if in_stock_only:
        counts = counts.where(Product.in_stock.is_(True))
    if category_ids is not None:
        counts = counts.where(Product.category_id.in_(category_ids))
    sub = counts.subquery()
    stmt = (
        select(Brand, func.coalesce(sub.c.n, 0))
        .outerjoin(sub, sub.c.brand_id == Brand.id)
        .where(Brand.is_active.is_(True))
        .order_by(Brand.sort_order, Brand.name, Brand.id)
    )
    if category_ids is not None:
        stmt = stmt.where(sub.c.n > 0)
    return [(brand, int(n)) for brand, n in (await session.execute(stmt)).all()]


async def visible_brand_by_slug(
    session: AsyncSession, slug: str, *, in_stock_only: bool = False
) -> tuple[Brand, int] | None:
    conditions = [Product.brand_id == Brand.id, visible_product_condition()]
    if in_stock_only:
        conditions.append(Product.in_stock.is_(True))
    count = select(func.count(Product.id)).where(*conditions).correlate(Brand).scalar_subquery()
    row = (await session.execute(select(Brand, count).where(Brand.slug == slug, Brand.is_active.is_(True)))).first()
    return (row[0], int(row[1])) if row else None


async def admin_list(
    session: AsyncSession, *, search: str | None, is_active: bool | None, offset: int, limit: int
) -> tuple[list[tuple[Brand, int]], int]:
    filters = []
    if search:
        filters.append(Brand.name.contains(search, autoescape=True) | Brand.slug.contains(search, autoescape=True))
    if is_active is not None:
        filters.append(Brand.is_active.is_(is_active))
    count = select(func.count(Product.id)).where(Product.brand_id == Brand.id).correlate(Brand).scalar_subquery()
    rows = await session.execute(
        select(Brand, count, func.count().over().label("total"))
        .where(*filters)
        .order_by(Brand.sort_order, Brand.name, Brand.id)
        .offset(offset)
        .limit(limit)
    )
    result = rows.all()
    if result:
        return [(r[0], int(r[1])) for r in result], int(result[0][2])
    total = int(await session.scalar(select(func.count(Brand.id)).where(*filters)) or 0)
    return [], total


async def count_products(session: AsyncSession, brand_id: int) -> int:
    return int(await session.scalar(select(func.count(Product.id)).where(Product.brand_id == brand_id)) or 0)


async def product_ids(session: AsyncSession, brand_id: int) -> list[int]:
    return list((await session.scalars(select(Product.id).where(Product.brand_id == brand_id))).all())
