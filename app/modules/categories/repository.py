from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.categories.models import Category, CategoryAttribute, CategoryVariantOption
from app.modules.products.models import Product
from app.modules.products.visibility import visible_product_condition


async def get_category(session: AsyncSession, category_id: int, *, for_update: bool = False) -> Category | None:
    stmt = select(Category).where(Category.id == category_id)
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def count_children(session: AsyncSession, category_id: int) -> int:
    return int(await session.scalar(select(func.count(Category.id)).where(Category.parent_id == category_id)) or 0)


async def child_ids(session: AsyncSession, category_id: int) -> list[int]:
    return list((await session.scalars(select(Category.id).where(Category.parent_id == category_id))).all())


async def count_products(session: AsyncSession, category_ids: Sequence[int]) -> int:
    return int(
        await session.scalar(select(func.count(Product.id)).where(Product.category_id.in_(category_ids))) or 0
    )


async def product_ids_in(session: AsyncSession, category_ids: Sequence[int]) -> list[int]:
    return list((await session.scalars(select(Product.id).where(Product.category_id.in_(category_ids)))).all())


async def product_counts_by_category(session: AsyncSession, *, visible_only: bool) -> dict[int, int]:
    """Number of products directly in each category (visible ones only, for customers)."""
    stmt = select(Product.category_id, func.count(Product.id)).group_by(Product.category_id)
    if visible_only:
        stmt = stmt.where(visible_product_condition())
    return {category_id: int(count) for category_id, count in (await session.execute(stmt)).all()}


async def get_attribute(session: AsyncSession, category_id: int, attribute_id: int) -> CategoryAttribute | None:
    return await session.scalar(
        select(CategoryAttribute).where(CategoryAttribute.id == attribute_id, CategoryAttribute.category_id == category_id)
    )


async def get_variant_option(session: AsyncSession, category_id: int, option_id: int) -> CategoryVariantOption | None:
    return await session.scalar(
        select(CategoryVariantOption).where(
            CategoryVariantOption.id == option_id, CategoryVariantOption.category_id == category_id
        )
    )


async def attribute_key_exists(session: AsyncSession, category_id: int, key: str) -> bool:
    stmt = select(CategoryAttribute.id).where(CategoryAttribute.category_id == category_id, CategoryAttribute.key == key)
    return await session.scalar(stmt.limit(1)) is not None


async def variant_option_key_exists(session: AsyncSession, category_id: int, key: str) -> bool:
    stmt = select(CategoryVariantOption.id).where(
        CategoryVariantOption.category_id == category_id, CategoryVariantOption.key == key
    )
    return await session.scalar(stmt.limit(1)) is not None


async def own_definitions(
    session: AsyncSession, category_id: int
) -> tuple[list[CategoryAttribute], list[CategoryVariantOption]]:
    attrs = await session.scalars(
        select(CategoryAttribute)
        .where(CategoryAttribute.category_id == category_id)
        .order_by(CategoryAttribute.sort_order, CategoryAttribute.id)
    )
    opts = await session.scalars(
        select(CategoryVariantOption)
        .where(CategoryVariantOption.category_id == category_id)
        .order_by(CategoryVariantOption.sort_order, CategoryVariantOption.id)
    )
    return list(attrs.all()), list(opts.all())
