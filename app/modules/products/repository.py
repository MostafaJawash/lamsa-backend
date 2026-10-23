"""Product queries. Each public read helper is ONE SQL statement (see the query budget tests)."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    case,
    cast,
    func,
    literal,
    null,
    select,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.modules.brands.models import Brand
from app.modules.brands.schemas import BrandRef
from app.modules.categories.definitions import AttributeDef, VariantOptionDef
from app.modules.categories.enums import AttributeType
from app.modules.categories.models import Category
from app.modules.categories.schemas import CategoryRef
from app.modules.products.enums import ProductType
from app.modules.products.filters import NUMBER, ProductFilters, build_conditions, search_condition
from app.modules.products.models import BundleItem, Product, ProductImage, ProductVariant
from app.modules.products.presenters import CardRow, LoadedImage, LoadedVariant
from app.modules.products.schemas import ProductSort
from app.modules.products.summary import BundleComponent, bundle_stock
from app.modules.products.visibility import visible_product_condition
from app.modules.settings.service import setting_subquery
from app.shared.catalog import json_path

CARD_COLUMNS = (
    Product.id, Product.slug, Product.name, Product.type, Product.price, Product.sale_price, Product.price_from,
    Product.is_new, Product.is_on_sale, Product.in_stock,
    Brand.id.label("brand_id"), Brand.name.label("brand_name"), Brand.slug.label("brand_slug"),
    Category.id.label("category_id"), Category.name.label("category_name"), Category.slug.label("category_slug"),
)


def card_row(r: Any) -> CardRow:
    return CardRow(
        id=r.id, slug=r.slug, name=r.name, type=r.type, price=r.price, sale_price=r.sale_price,
        price_from=r.price_from, is_new=r.is_new, is_on_sale=r.is_on_sale, in_stock=r.in_stock,
        brand=BrandRef(id=r.brand_id, name=r.brand_name, slug=r.brand_slug) if r.brand_id is not None else None,
        category=CategoryRef(id=r.category_id, name=r.category_name, slug=r.category_slug),
    )


def card_select(*extra: Any) -> Select[Any]:
    return (
        select(*CARD_COLUMNS, *extra)
        .join(Category, Category.id == Product.category_id)
        .outerjoin(Brand, Brand.id == Product.brand_id)
    )


def _order(sort: ProductSort, filters: ProductFilters) -> list[Any]:
    if sort == ProductSort.relevance and filters.search:
        return [search_condition(filters.search).desc(), Product.id.desc()]
    if sort == ProductSort.price_asc:
        return [Product.price_from.asc(), Product.id.desc()]
    if sort == ProductSort.price_desc:
        return [Product.price_from.desc(), Product.id.desc()]
    return [Product.created_at.desc(), Product.id.desc()]


async def list_cards(
    session: AsyncSession, filters: ProductFilters, sort: ProductSort, offset: int, limit: int
) -> tuple[list[CardRow], int | None]:
    """One page of cards plus the total via COUNT(*) OVER() (None when the page is empty)."""
    rows = (
        await session.execute(
            card_select(func.count().over().label("total"))
            .where(*build_conditions(filters))
            .order_by(*_order(sort, filters))
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return [card_row(r) for r in rows], (int(rows[0].total) if rows else None)


async def count_products(session: AsyncSession, filters: ProductFilters) -> int:
    return int(await session.scalar(select(func.count(Product.id)).where(*build_conditions(filters))) or 0)


async def main_image_keys(session: AsyncSession, product_ids: Sequence[int]) -> dict[int, str]:
    if not product_ids:
        return {}
    rn = func.row_number().over(partition_by=ProductImage.product_id, order_by=(ProductImage.sort_order, ProductImage.id))
    sub = (
        select(ProductImage.product_id, ProductImage.image_key, rn.label("rn"))
        .where(ProductImage.product_id.in_(product_ids))
        .subquery()
    )
    rows = await session.execute(select(sub.c.product_id, sub.c.image_key).where(sub.c.rn == 1))
    return {r.product_id: r.image_key for r in rows}


def loaded_variant(v: Any) -> LoadedVariant:
    return LoadedVariant(
        id=v.id, product_id=v.product_id, options=dict(v.options or {}), price=v.price, sale_price=v.sale_price,
        stock=v.stock, is_active=v.is_active, sort_order=v.sort_order,
    )


async def variants_of(
    session: AsyncSession, product_ids: Sequence[int], *, active_only: bool
) -> dict[int, list[LoadedVariant]]:
    if not product_ids:
        return {}
    stmt = select(
        ProductVariant.id, ProductVariant.product_id, ProductVariant.options, ProductVariant.price,
        ProductVariant.sale_price, ProductVariant.stock, ProductVariant.is_active, ProductVariant.sort_order,
    ).where(ProductVariant.product_id.in_(product_ids))
    if active_only:
        stmt = stmt.where(ProductVariant.is_active.is_(True))
    result: dict[int, list[LoadedVariant]] = {}
    for v in await session.execute(stmt.order_by(ProductVariant.product_id, ProductVariant.sort_order, ProductVariant.id)):
        result.setdefault(v.product_id, []).append(loaded_variant(v))
    return result


# ---------------------------------------------------------------------------------- detail


@dataclass(frozen=True)
class DetailRow:
    product: Product
    brand: Brand | None
    threshold_raw: Any


async def visible_product_by_slug(session: AsyncSession, slug: str) -> DetailRow | None:
    row = (
        await session.execute(
            select(Product, Brand, setting_subquery("low_stock_threshold").label("threshold"))
            .outerjoin(Brand, Brand.id == Product.brand_id)
            .where(Product.slug == slug, visible_product_condition())
        )
    ).first()
    return DetailRow(product=row[0], brand=row[1], threshold_raw=row[2]) if row else None


async def images_of(session: AsyncSession, product_id: int) -> list[LoadedImage]:
    rows = await session.execute(
        select(
            ProductImage.id, ProductImage.variant_id, ProductImage.image_key, ProductImage.alt_text,
            ProductImage.width, ProductImage.height, ProductImage.sort_order,
        )
        .where(ProductImage.product_id == product_id)
        .order_by(ProductImage.sort_order, ProductImage.id)
    )
    return [LoadedImage(r.id, r.variant_id, r.image_key, r.alt_text, r.width, r.height, r.sort_order) for r in rows]


@dataclass(frozen=True)
class BundleComponentRow:
    item_id: int
    quantity: int
    product_id: int
    product_slug: str
    product_name: str
    product_active: bool
    product_stock: int | None
    variant_id: int | None
    variant_options: dict[str, Any] | None
    variant_stock: int | None
    variant_active: bool | None
    image_key: str | None

    @property
    def stock(self) -> int:
        return (self.variant_stock if self.variant_id is not None else self.product_stock) or 0

    @property
    def active(self) -> bool:
        return self.product_active and (self.variant_id is None or bool(self.variant_active))


async def bundle_components(session: AsyncSession, bundle_id: int) -> list[BundleComponentRow]:
    return (await bundle_components_for(session, [bundle_id])).get(bundle_id, [])


async def bundle_components_for(
    session: AsyncSession, bundle_ids: Sequence[int]
) -> dict[int, list[BundleComponentRow]]:
    """Items of several bundles with their component product/variant and main image, in ONE query."""
    if not bundle_ids:
        return {}
    image = (
        select(ProductImage.image_key)
        .where(ProductImage.product_id == BundleItem.product_id)
        .order_by(ProductImage.sort_order, ProductImage.id)
        .limit(1)
        .correlate(BundleItem)
        .scalar_subquery()
    )
    rows = await session.execute(
        select(
            BundleItem.bundle_product_id, BundleItem.id, BundleItem.quantity, Product.id.label("product_id"),
            Product.slug, Product.name,
            Product.is_active, Product.stock, ProductVariant.id.label("variant_id"), ProductVariant.options,
            ProductVariant.stock.label("variant_stock"), ProductVariant.is_active.label("variant_active"),
            image.label("image_key"),
        )
        .join(Product, Product.id == BundleItem.product_id)
        .outerjoin(ProductVariant, ProductVariant.id == BundleItem.variant_id)
        .where(BundleItem.bundle_product_id.in_(bundle_ids))
        .order_by(BundleItem.bundle_product_id, BundleItem.id)
    )
    result: dict[int, list[BundleComponentRow]] = {}
    for r in rows:
        result.setdefault(r.bundle_product_id, []).append(
            BundleComponentRow(
                item_id=r.id, quantity=r.quantity, product_id=r.product_id, product_slug=r.slug,
                product_name=r.name, product_active=r.is_active, product_stock=r.stock, variant_id=r.variant_id,
                variant_options=r.options, variant_stock=r.variant_stock, variant_active=r.variant_active,
                image_key=r.image_key,
            )
        )
    return result


def derived_bundle_stock(components: Sequence[BundleComponentRow]) -> int:
    """Bundle stock from loaded components (the rule lives in summary.bundle_stock)."""
    return bundle_stock([BundleComponent(c.quantity, c.stock, c.active) for c in components])


async def related_cards(
    session: AsyncSession, category_ids: Sequence[int], exclude_ids: Sequence[int], limit: int
) -> list[CardRow]:
    rows = await session.execute(
        card_select()
        .where(
            visible_product_condition(),
            Product.in_stock.is_(True),
            Product.category_id.in_(category_ids),
            Product.id.not_in(exclude_ids),
        )
        .order_by(Product.created_at.desc(), Product.id.desc())
        .limit(limit)
    )
    return [card_row(r) for r in rows]


# ---------------------------------------------------------------------------------- facets


async def brand_facet(session: AsyncSession, filters: ProductFilters) -> list[tuple[str, str, int]]:
    rows = await session.execute(
        select(Brand.slug, Brand.name, func.count(Product.id))
        .join(Brand, Brand.id == Product.brand_id)
        .where(*build_conditions(filters, exclude="brand"))
        .group_by(Brand.id, Brand.slug, Brand.name, Brand.sort_order)
        .order_by(Brand.sort_order, Brand.name)
    )
    return [(r[0], r[1], int(r[2])) for r in rows]


async def price_facet(session: AsyncSession, filters: ProductFilters) -> tuple[Decimal | None, Decimal | None]:
    row = (
        await session.execute(
            select(func.min(Product.price_from), func.max(Product.price_from)).where(
                *build_conditions(filters, exclude="price")
            )
        )
    ).one()
    return row[0], row[1]


@dataclass(frozen=True)
class FacetRow:
    key: str
    value: str | None
    count: int
    min: Decimal | None
    max: Decimal | None
    hex: str | None = None


async def attribute_facets(
    session: AsyncSession, filters: ProductFilters, definitions: Sequence[AttributeDef]
) -> list[FacetRow]:
    """All attribute facets in ONE statement (UNION ALL), each ignoring its own filter."""
    parts: list[Select[Any]] = []
    for d in definitions:
        path = json_path(d.key)
        extracted = func.json_extract(Product.attributes, path)
        has_key = func.json_contains_path(Product.attributes, "one", path) == 1
        conditions = [*build_conditions(filters, exclude=f"attr:{d.key}"), has_key]
        if d.type == AttributeType.number:
            number = cast(extracted, NUMBER)
            parts.append(
                select(
                    literal(d.key).label("k"), null().label("v"), func.count(Product.id).label("n"),
                    func.min(number).label("mn"), func.max(number).label("mx"),
                ).where(*conditions)
            )
        elif d.type == AttributeType.multiselect:
            # One row per option value: products whose list contains it.
            for value in d.options or []:
                parts.append(
                    select(
                        literal(d.key).label("k"), literal(value).label("v"),
                        func.count(Product.id).label("n"), null().label("mn"), null().label("mx"),
                    ).where(*conditions, func.json_contains(extracted, func.json_quote(value)) == 1)
                )
        else:  # select, boolean
            value_col = func.json_unquote(extracted)
            parts.append(
                select(
                    literal(d.key).label("k"), value_col.label("v"), func.count(Product.id).label("n"),
                    null().label("mn"), null().label("mx"),
                )
                .where(*conditions)
                .group_by(value_col)
            )
    if not parts:
        return []
    rows = await session.execute(union_all(*parts))
    return [FacetRow(r.k, r.v, int(r.n), r.mn, r.mx) for r in rows]


async def option_facets(
    session: AsyncSession, filters: ProductFilters, definitions: Sequence[VariantOptionDef]
) -> list[FacetRow]:
    """All variant-option facets in ONE statement (UNION ALL), over active variants."""
    parts: list[Select[Any]] = []
    for d in definitions:
        variant = aliased(ProductVariant)
        value_col = func.json_unquote(func.json_extract(variant.options, json_path(d.key)))
        hex_col = func.min(func.json_unquote(func.json_extract(variant.options, json_path(d.hex_key))))
        parts.append(
            select(
                literal(d.key).label("k"), value_col.label("v"), func.count(func.distinct(Product.id)).label("n"),
                hex_col.label("hx"),
            )
            .join(variant, and_(variant.product_id == Product.id, variant.is_active.is_(True)))
            .where(*build_conditions(filters, exclude=f"opt:{d.key}"), value_col.is_not(None))
            .group_by(value_col)
        )
    if not parts:
        return []
    rows = await session.execute(union_all(*parts))
    return [FacetRow(r.k, r.v, int(r.n), None, None, r.hx) for r in rows]


# ----------------------------------------------------------------------------------- admin


def stock_expression() -> ColumnElement[Any]:
    """Simple: stock; variable: sum of variant stock; bundle: NULL."""
    variant_sum = (
        select(func.coalesce(func.sum(ProductVariant.stock), 0))
        .where(ProductVariant.product_id == Product.id)
        .correlate(Product)
        .scalar_subquery()
    )
    return case(
        (Product.type == ProductType.simple.value, Product.stock),
        (Product.type == ProductType.variable.value, variant_sum),
        else_=null(),
    )
