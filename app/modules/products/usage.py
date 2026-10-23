"""How products and variants use attribute / variant-option definitions (for definition rules).

JSON paths are built by json_path() from whitelisted keys and always sent as bound parameters.
"""

from collections.abc import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.products.models import Product, ProductVariant
from app.shared.catalog import json_path


def _has_key(column: object, key: str) -> object:
    return func.json_contains_path(column, "one", json_path(key)) == 1


async def products_using_attribute(session: AsyncSession, category_ids: Sequence[int], key: str) -> list[int]:
    rows = await session.scalars(
        select(Product.id).where(Product.category_id.in_(category_ids), _has_key(Product.attributes, key))
    )
    return list(rows.all())


async def count_products_using_attribute_value(
    session: AsyncSession, category_ids: Sequence[int], key: str, value: str, *, multiselect: bool
) -> int:
    extracted = func.json_extract(Product.attributes, json_path(key))
    if multiselect:
        condition = func.json_contains(extracted, func.json_quote(value)) == 1
    else:
        condition = func.json_unquote(extracted) == value
    return int(
        await session.scalar(
            select(func.count(Product.id)).where(Product.category_id.in_(category_ids), _has_key(Product.attributes, key), condition)
        )
        or 0
    )


async def remove_attribute_key(session: AsyncSession, category_ids: Sequence[int], key: str) -> list[int]:
    """JSON_REMOVE the key from every product of the categories; returns the affected ids."""
    ids = await products_using_attribute(session, category_ids, key)
    if ids:
        await session.execute(
            update(Product)
            .where(Product.id.in_(ids))
            .values(attributes=func.json_remove(Product.attributes, json_path(key)))
            .execution_options(synchronize_session=False)
        )
    return ids


async def count_variants_using_option(session: AsyncSession, category_ids: Sequence[int], key: str) -> int:
    return int(
        await session.scalar(
            select(func.count(ProductVariant.id))
            .join(Product, Product.id == ProductVariant.product_id)
            .where(Product.category_id.in_(category_ids), _has_key(ProductVariant.options, key))
        )
        or 0
    )


async def count_variants_using_option_value(
    session: AsyncSession, category_ids: Sequence[int], key: str, value: str
) -> int:
    return int(
        await session.scalar(
            select(func.count(ProductVariant.id))
            .join(Product, Product.id == ProductVariant.product_id)
            .where(
                Product.category_id.in_(category_ids),
                func.json_unquote(func.json_extract(ProductVariant.options, json_path(key))) == value,
            )
        )
        or 0
    )
