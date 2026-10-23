"""Catalog helpers shared by categories, brands and products."""

import json
import re
from collections.abc import Collection
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.products.models import Product, ProductVariant
from app.modules.settings.models import StoreSetting
from app.shared.exceptions import Conflict, ErrorCode, ValidationFailed
from app.shared.utils.slug import is_valid_slug, slug_from_name, with_suffix

DEFINITION_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,49}$")
SKU_PATTERN = re.compile(r"^[A-Z0-9-]{3,64}$")
DEFAULT_LOW_STOCK_THRESHOLD = 3
LOW_STOCK_SETTING = "low_stock_threshold"


def json_path(key: str) -> str:
    """JSON path for a definition key. Keys are whitelisted by pattern before reaching SQL, and
    the path itself is always sent as a bound parameter, never written into the SQL text."""
    if not DEFINITION_KEY_PATTERN.match(key) and not (
        key.endswith("_hex") and DEFINITION_KEY_PATTERN.match(key[:-4])
    ):
        raise ValueError(f"invalid definition key {key!r}")
    return f'$."{key}"'


def field_error(field: str, message: str, code: ErrorCode = ErrorCode.VALIDATION_ERROR) -> ValidationFailed:
    return ValidationFailed(code, details=[{"field": field, "message": message}])


async def unique_slug(
    session: AsyncSession,
    model: Any,
    *,
    slug: str | None,
    name: str,
    fallback_prefix: str,
    exclude_id: int | None = None,
    reserved: Collection[str] = (),
) -> str:
    """Explicit slug: validate, 409 SLUG_TAKEN if used. Omitted: from the name, then -2, -3..."""
    if slug is not None:
        if not is_valid_slug(slug):
            raise field_error("slug", "lowercase letters, digits and dashes, 2..120 characters")
        if slug in reserved or await _slug_exists(session, model, slug, exclude_id):
            raise Conflict(ErrorCode.SLUG_TAKEN, details={"slug": slug})
        return slug

    base = slug_from_name(name, fallback_prefix)
    stmt = select(model.slug).where(or_(model.slug == base, model.slug.like(f"{base}-%")))
    if exclude_id is not None:
        stmt = stmt.where(model.id != exclude_id)
    taken = set((await session.scalars(stmt)).all()) | set(reserved)
    n = 1
    while with_suffix(base, n) in taken:
        n += 1
    return with_suffix(base, n)


async def _slug_exists(session: AsyncSession, model: Any, slug: str, exclude_id: int | None) -> bool:
    stmt = select(model.id).where(model.slug == slug)
    if exclude_id is not None:
        stmt = stmt.where(model.id != exclude_id)
    return await session.scalar(stmt.limit(1)) is not None


def normalize_sku(sku: str) -> str:
    value = sku.strip().upper()
    if not SKU_PATTERN.match(value):
        raise field_error("sku", "uppercase letters, digits and dashes, 3..64 characters")
    return value


async def sku_taken(
    session: AsyncSession, sku: str, *, product_id: int | None = None, variant_id: int | None = None
) -> bool:
    """SKUs are unique across products.sku and product_variants.sku."""
    product_stmt = select(Product.id).where(Product.sku == sku)
    if product_id is not None:
        product_stmt = product_stmt.where(Product.id != product_id)
    if await session.scalar(product_stmt.limit(1)) is not None:
        return True
    variant_stmt = select(ProductVariant.id).where(ProductVariant.sku == sku)
    if variant_id is not None:
        variant_stmt = variant_stmt.where(ProductVariant.id != variant_id)
    return await session.scalar(variant_stmt.limit(1)) is not None


async def ensure_sku_available(
    session: AsyncSession, sku: str, *, product_id: int | None = None, variant_id: int | None = None
) -> None:
    if await sku_taken(session, sku, product_id=product_id, variant_id=variant_id):
        raise Conflict(ErrorCode.SKU_TAKEN, details={"sku": sku})


def map_catalog_integrity_error(exc: IntegrityError) -> Conflict | None:
    """Translate UNIQUE violations raced past the service checks into the right 409."""
    text = str(exc.orig)
    if "uq_products_sku" in text or "uq_product_variants_sku" in text:
        return Conflict(ErrorCode.SKU_TAKEN)
    if "_slug" in text:
        return Conflict(ErrorCode.SLUG_TAKEN)
    if "uq_product_variants_product_id_options_signature" in text:
        return Conflict(ErrorCode.DUPLICATE_VARIANT)
    if "uq_category_attributes_category_id_key" in text or "uq_category_variant_options_category_id_key" in text:
        return Conflict(ErrorCode.ATTRIBUTE_KEY_TAKEN)
    return None


async def flush_catalog(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        conflict = map_catalog_integrity_error(exc)
        if conflict is None:
            raise
        raise conflict from exc


async def get_low_stock_threshold(session: AsyncSession) -> int:
    value = await session.scalar(select(StoreSetting.value).where(StoreSetting.key == LOW_STOCK_SETTING))
    return parse_threshold(value)


def low_stock_threshold_subquery() -> Any:
    """Scalar subquery so the threshold rides along in another query (no extra statement)."""
    return select(StoreSetting.value).where(StoreSetting.key == LOW_STOCK_SETTING).scalar_subquery()


def parse_threshold(value: Any) -> int:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return DEFAULT_LOW_STOCK_THRESHOLD
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return DEFAULT_LOW_STOCK_THRESHOLD
    return value
