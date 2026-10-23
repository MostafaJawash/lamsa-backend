"""Stock deduction (placement) and restoration (cancellation).

Locking order, always: products by ascending id, THEN variants by ascending id (the admin
stock-adjust endpoints lock product then variant as well), so two transactions never wait on
each other in a cycle.

Which rows to lock is decided by plain reads beforehand. If such a read misses a row that turns
out to be needed (e.g. a bundle created a moment ago now contains one of our products), the
missed row is simply locked later, out of order: at worst that causes a deadlock, which MySQL
breaks and the service retries once. It can never cause wrong stock: every deduction is ONE
conditional UPDATE (stock >= quantity, checked in SQL) whose affected-row count is verified.
"""

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Integer, case, cast, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.orders.models import OrderItem
from app.modules.products.models import BundleItem, Product, ProductVariant
from app.shared.events import StockLow
from app.shared.exceptions import insufficient_stock

logger = logging.getLogger(__name__)


@dataclass
class StockNeeds:
    """Quantities per product (simple products, bundle components) and per variant, summed."""

    products: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    variants: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    variant_product: dict[int, int] = field(default_factory=dict)  # variant -> its product

    def add_product(self, product_id: int, quantity: int) -> None:
        self.products[product_id] += quantity

    def add_variant(self, product_id: int, variant_id: int, quantity: int) -> None:
        self.variants[variant_id] += quantity
        self.variant_product[variant_id] = product_id

    @property
    def product_ids(self) -> set[int]:
        """Every product whose summary may change (variant owners included)."""
        return set(self.products) | set(self.variant_product.values())


@dataclass(frozen=True)
class LockedStock:
    products: dict[int, int | None]  # id -> current stock (locked, current values)
    variants: dict[int, int]


async def bundles_containing(session: AsyncSession, product_ids: Iterable[int]) -> set[int]:
    ids = list(product_ids)
    if not ids:
        return set()
    rows = await session.scalars(select(BundleItem.bundle_product_id).where(BundleItem.product_id.in_(ids)).distinct())
    return set(rows.all())


async def lock_rows(session: AsyncSession, product_ids: Iterable[int], variant_ids: Iterable[int]) -> LockedStock:
    """SELECT ... FOR UPDATE: products by ascending id, then variants by ascending id. Returns the
    current stock of the rows that exist (missing ids are simply absent)."""
    products: dict[int, int | None] = {}
    variants: dict[int, int] = {}
    p_ids = sorted(set(product_ids))
    v_ids = sorted(set(variant_ids))
    if p_ids:
        rows = await session.execute(
            select(Product.id, Product.stock).where(Product.id.in_(p_ids)).order_by(Product.id).with_for_update()
        )
        products = {r.id: r.stock for r in rows}
    if v_ids:
        rows = await session.execute(
            select(ProductVariant.id, ProductVariant.stock)
            .where(ProductVariant.id.in_(v_ids))
            .order_by(ProductVariant.id)
            .with_for_update()
        )
        variants = {r.id: r.stock for r in rows}
    return LockedStock(products=products, variants=variants)


def _case(values: Mapping[int, int], model_id: Any) -> Any:
    """CASE id WHEN <id> THEN <quantity> ... END: one statement for all rows."""
    return cast(case(dict(values), value=model_id, else_=0), Integer)


async def deduct(
    session: AsyncSession, needs: StockNeeds, locked: LockedStock, threshold: int
) -> list[StockLow]:
    """Take stock in ONE UPDATE per table. Raises INSUFFICIENT_STOCK (409) if anything is short.
    Returns StockLow events for rows that crossed the low-stock threshold."""
    for product_id, quantity in sorted(needs.products.items()):
        available = locked.products.get(product_id) or 0
        if available < quantity:
            raise insufficient_stock(product_id, None, available)
    for variant_id, quantity in sorted(needs.variants.items()):
        available = locked.variants.get(variant_id, 0)
        if available < quantity:
            raise insufficient_stock(needs.variant_product[variant_id], variant_id, available)

    if needs.products:
        q = _case(needs.products, Product.id)
        result = await session.execute(
            update(Product)
            .where(Product.id.in_(list(needs.products)), Product.stock >= q)
            .values(stock=Product.stock - q)
            .execution_options(synchronize_session=False)
        )
        _check_rowcount(result, len(needs.products), "products")
    if needs.variants:
        q = _case(needs.variants, ProductVariant.id)
        result = await session.execute(
            update(ProductVariant)
            .where(ProductVariant.id.in_(list(needs.variants)), ProductVariant.stock >= q)
            .values(stock=ProductVariant.stock - q)
            .execution_options(synchronize_session=False)
        )
        _check_rowcount(result, len(needs.variants), "variants")

    events: list[StockLow] = []
    for product_id, quantity in needs.products.items():
        before = locked.products.get(product_id) or 0
        if before > threshold >= before - quantity:
            events.append(StockLow(product_id=product_id, variant_id=None, stock=before - quantity, threshold=threshold))
    for variant_id, quantity in needs.variants.items():
        before = locked.variants.get(variant_id, 0)
        if before > threshold >= before - quantity:
            events.append(StockLow(product_id=needs.variant_product[variant_id], variant_id=variant_id,
                                   stock=before - quantity, threshold=threshold))
    return events


def _check_rowcount(result: Any, expected: int, table: str) -> None:
    """With the rows locked and checked above this cannot fail; if it ever does, abort rather
    than leave a partial deduction (the transaction is rolled back by the caller)."""
    if result.rowcount != expected:
        logger.error("Stock deduction on %s touched %s rows, expected %s", table, result.rowcount, expected)
        raise RuntimeError(f"stock deduction mismatch on {table}")


def needs_from_order_items(items: Sequence[OrderItem]) -> tuple[StockNeeds, list[str]]:
    """What an order took: simple lines, variants, and bundle components (x line quantity).
    Returns the needs and notes about items that can't be restored (product deleted)."""
    needs = StockNeeds()
    skipped: list[str] = []
    for item in items:
        if item.bundle_contents is not None:
            for component in item.bundle_contents:
                product_id, variant_id = component.get("product_id"), component.get("variant_id")
                quantity = int(component.get("quantity", 1)) * item.quantity
                if variant_id is not None and product_id is not None:
                    needs.add_variant(product_id, variant_id, quantity)
                elif product_id is not None:
                    needs.add_product(product_id, quantity)
        elif item.variant_id is not None and item.product_id is not None:
            needs.add_variant(item.product_id, item.variant_id, item.quantity)
        elif item.product_id is not None and item.options is None:
            # A simple product (variable lines always have options; a variable line whose variant
            # was deleted has variant_id NULL but options set, and is skipped below).
            needs.add_product(item.product_id, item.quantity)
        else:
            skipped.append(f"order item {item.id} ({item.product_name}): product or variant no longer exists")
    return needs, skipped


async def restore(session: AsyncSession, needs: StockNeeds, order_id: int) -> set[int]:
    """Add back exactly what was taken. Locks rows in id order; rows that no longer exist are
    skipped and logged. Returns the product ids whose summaries must be recalculated."""
    bundles = await bundles_containing(session, needs.product_ids)
    locked = await lock_rows(session, needs.product_ids | bundles, needs.variants)
    products = {p: q for p, q in needs.products.items() if p in locked.products}
    variants = {v: q for v, q in needs.variants.items() if v in locked.variants}
    for missing in sorted(set(needs.products) - set(products)):
        logger.warning("Order %s: product %s no longer exists, its stock was not restored", order_id, missing)
    for missing in sorted(set(needs.variants) - set(variants)):
        logger.warning("Order %s: variant %s no longer exists, its stock was not restored", order_id, missing)
    if products:
        q = _case(products, Product.id)
        await session.execute(
            update(Product).where(Product.id.in_(list(products)))
            .values(stock=Product.stock + q).execution_options(synchronize_session=False)
        )
    if variants:
        q = _case(variants, ProductVariant.id)
        await session.execute(
            update(ProductVariant).where(ProductVariant.id.in_(list(variants)))
            .values(stock=ProductVariant.stock + q).execution_options(synchronize_session=False)
        )
    return set(products) | {needs.variant_product[v] for v in variants if needs.variant_product.get(v) in locked.products}
