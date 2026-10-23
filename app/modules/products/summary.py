"""Derived product fields: price_from, in_stock, is_on_sale, and search_text.

recalculate_product_summary() is the single source of truth for the summary fields. Call it
after every change that can affect them; it also recalculates every bundle containing any of
the given products. refresh_search_text() rebuilds the normalized search text.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.brands.models import Brand
from app.modules.categories.definitions import load_definition_rows, merge_definitions
from app.modules.categories.enums import AttributeType
from app.modules.categories.models import Category
from app.modules.products.enums import ProductType
from app.modules.products.models import BundleItem, Product, ProductVariant
from app.shared.utils.arabic import normalize_arabic

ZERO = Decimal("0.00")
SEARCHABLE_ATTRIBUTE_TYPES = {AttributeType.text, AttributeType.select, AttributeType.multiselect}


@dataclass(frozen=True)
class VariantRow:
    id: int
    price: Decimal
    sale_price: Decimal | None
    stock: int
    is_active: bool
    sort_order: int = 0

    @property
    def current(self) -> Decimal:
        return self.sale_price if self.sale_price is not None else self.price


@dataclass(frozen=True)
class Summary:
    price_from: Decimal
    in_stock: bool
    is_on_sale: bool


def pick_variant_for_price(variants: Sequence[VariantRow]) -> VariantRow | None:
    """The active variant whose current price is price_from (in-stock ones preferred)."""
    active = [v for v in variants if v.is_active]
    pool = [v for v in active if v.stock > 0] or active
    return min(pool, key=lambda v: (v.current, v.sort_order, v.id)) if pool else None


def summarize_simple(price: Decimal | None, sale_price: Decimal | None, stock: int | None) -> Summary:
    current = sale_price if sale_price is not None else (price or ZERO)
    return Summary(price_from=current, in_stock=(stock or 0) > 0, is_on_sale=sale_price is not None)


def summarize_variable(variants: Sequence[VariantRow]) -> Summary:
    chosen = pick_variant_for_price(variants)
    active = [v for v in variants if v.is_active]
    return Summary(
        price_from=chosen.current if chosen else ZERO,
        in_stock=any(v.stock > 0 for v in active),
        is_on_sale=any(v.sale_price is not None for v in active),
    )


@dataclass(frozen=True)
class BundleComponent:
    quantity: int
    stock: int
    is_active: bool  # component product (and variant, if any) active


def bundle_stock(components: Sequence[BundleComponent]) -> int:
    """min over items of floor(item_stock / quantity); an inactive component makes it 0."""
    if not components:
        return 0
    return min((c.stock // c.quantity) if c.is_active else 0 for c in components)


async def load_bundle_components(session: AsyncSession, bundle_ids: Sequence[int]) -> dict[int, list[BundleComponent]]:
    if not bundle_ids:
        return {}
    rows = await session.execute(
        select(
            BundleItem.bundle_product_id,
            BundleItem.quantity,
            Product.stock,
            Product.is_active,
            ProductVariant.id.label("variant_id"),
            ProductVariant.stock.label("variant_stock"),
            ProductVariant.is_active.label("variant_active"),
        )
        .join(Product, Product.id == BundleItem.product_id)
        .outerjoin(ProductVariant, ProductVariant.id == BundleItem.variant_id)
        .where(BundleItem.bundle_product_id.in_(bundle_ids))
    )
    result: dict[int, list[BundleComponent]] = {}
    for r in rows:
        if r.variant_id is not None:
            stock, active = r.variant_stock or 0, bool(r.is_active and r.variant_active)
        else:
            stock, active = r.stock or 0, bool(r.is_active)
        result.setdefault(r.bundle_product_id, []).append(BundleComponent(r.quantity, stock, active))
    return result


async def recalculate_product_summary(session: AsyncSession, product_ids: Iterable[int]) -> None:
    """Recompute price_from / in_stock / is_on_sale for the products and every bundle containing them."""
    ids = {int(i) for i in product_ids}
    if not ids:
        return
    await session.flush()
    containing = await session.scalars(
        select(BundleItem.bundle_product_id).where(BundleItem.product_id.in_(ids)).distinct()
    )
    ids |= set(containing.all())

    products = (
        await session.execute(
            select(Product.id, Product.type, Product.price, Product.sale_price, Product.stock).where(Product.id.in_(ids))
        )
    ).all()
    variable_ids = [p.id for p in products if p.type == ProductType.variable]
    bundle_ids = [p.id for p in products if p.type == ProductType.bundle]

    variants: dict[int, list[VariantRow]] = {}
    if variable_ids:
        for v in await session.execute(
            select(
                ProductVariant.product_id, ProductVariant.id, ProductVariant.price, ProductVariant.sale_price,
                ProductVariant.stock, ProductVariant.is_active, ProductVariant.sort_order,
            ).where(ProductVariant.product_id.in_(variable_ids))
        ):
            variants.setdefault(v.product_id, []).append(
                VariantRow(v.id, v.price, v.sale_price, v.stock, v.is_active, v.sort_order)
            )
    components = await load_bundle_components(session, bundle_ids)

    values: list[dict[str, Any]] = []
    for p in products:
        if p.type == ProductType.variable:
            summary = summarize_variable(variants.get(p.id, []))
        elif p.type == ProductType.bundle:
            base = summarize_simple(p.price, p.sale_price, None)
            stock = bundle_stock(components.get(p.id, []))
            summary = Summary(price_from=base.price_from, in_stock=stock > 0, is_on_sale=base.is_on_sale)
        else:
            summary = summarize_simple(p.price, p.sale_price, p.stock)
        values.append(
            {"id": p.id, "price_from": summary.price_from, "in_stock": summary.in_stock, "is_on_sale": summary.is_on_sale}
        )
    await session.execute(update(Product), values)
    _expire(session, Product, ids, ("price_from", "in_stock", "is_on_sale"))


def _expire(session: AsyncSession, model: type, ids: set[int], attrs: Sequence[str]) -> None:
    """Drop stale in-memory values of objects already loaded in this session."""
    for obj in list(session.identity_map.values()):
        if isinstance(obj, model) and getattr(obj, "id", None) in ids:
            session.sync_session.expire(obj, list(attrs))


async def refresh_search_text(session: AsyncSession, product_ids: Iterable[int]) -> None:
    """Rebuild products.search_text: normalized name, description, brand, category and parent
    category names, text/select/multiselect attribute values and active variant option values."""
    ids = {int(i) for i in product_ids}
    if not ids:
        return
    await session.flush()
    parent = Category.__table__.alias("parent_category")
    rows = (
        await session.execute(
            select(
                Product.id, Product.name, Product.description, Product.attributes, Product.category_id,
                Category.parent_id, Brand.name.label("brand_name"), Category.name.label("category_name"),
                parent.c.name.label("parent_name"),
            )
            .join(Category, Category.id == Product.category_id)
            .outerjoin(parent, parent.c.id == Category.parent_id)
            .outerjoin(Brand, Brand.id == Product.brand_id)
            .where(Product.id.in_(ids))
        )
    ).all()
    if not rows:
        return
    category_ids = {r.category_id for r in rows} | {r.parent_id for r in rows if r.parent_id}
    definition_rows = await load_definition_rows(session, sorted(category_ids))
    option_values: dict[int, list[str]] = {}
    for v in await session.execute(
        select(ProductVariant.product_id, ProductVariant.options).where(
            ProductVariant.product_id.in_(ids), ProductVariant.is_active.is_(True)
        )
    ):
        option_values.setdefault(v.product_id, []).extend(
            str(value) for key, value in (v.options or {}).items() if not key.endswith("_hex")
        )

    values = []
    for r in rows:
        definitions = merge_definitions(r.category_id, r.parent_id, definition_rows)
        parts: list[str] = [r.name, r.description or "", r.brand_name or "", r.category_name, r.parent_name or ""]
        for key, value in (r.attributes or {}).items():
            definition = definitions.attributes.get(key)
            if definition is None or definition.type not in SEARCHABLE_ATTRIBUTE_TYPES:
                continue
            parts.extend(value if isinstance(value, list) else [str(value)])
        parts.extend(dict.fromkeys(option_values.get(r.id, [])))
        values.append({"id": r.id, "search_text": normalize_arabic(" ".join(p for p in parts if p))})
    await session.execute(update(Product), values)
    _expire(session, Product, ids, ("search_text",))


async def refresh_products(session: AsyncSession, product_ids: Iterable[int]) -> None:
    """Both summary and search text (the usual call after a product change)."""
    ids = list(product_ids)
    await recalculate_product_summary(session, ids)
    await refresh_search_text(session, ids)
