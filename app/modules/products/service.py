"""Admin product management. Each public method is one transaction and commits itself.

After every change that can affect price_from / in_stock / is_on_sale or the search text,
refresh_products() is called (it also recalculates bundles containing the product).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, delete, exists, func, null, or_, select, union_all, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.storage.transaction import MediaTransaction
from app.modules.brands.models import Brand
from app.modules.brands.schemas import BrandRef
from app.modules.categories.definitions import EffectiveDefinitions, load_effective_definitions
from app.modules.categories.models import Category
from app.modules.categories.schemas import CategoryRef
from app.modules.orders.models import OrderItem
from app.modules.products import repository as repo
from app.modules.products.enums import ProductType
from app.modules.products.filters import search_condition
from app.modules.products.models import BundleItem, Product, ProductImage, ProductVariant
from app.modules.products.presenters import LoadedImage, colors_without_images, image_colors
from app.modules.products.schemas import (
    AdminBundleItemOut,
    AdminImageOut,
    AdminProductListItem,
    AdminProductOut,
    AdminProductSort,
    AdminVariantOut,
    BundleItemIn,
    ImageDeleteOut,
    ImagesUploadOut,
    ImageUpdate,
    LowStockItem,
    ProductCreate,
    ProductUpdate,
    ProductWarning,
    Publishable,
    StockOut,
    VariantIn,
    VariantUpdate,
)
from app.modules.products.summary import BundleComponent, bundle_stock, refresh_products
from app.modules.products.utils import build_options_signature
from app.modules.products.validation import (
    missing_required,
    validate_attributes,
    validate_variant_options,
    validate_variant_set,
    variant_label,
)
from app.shared.catalog import (
    ensure_sku_available,
    field_error,
    flush_catalog,
    get_low_stock_threshold,
    normalize_sku,
    sku_taken,
    unique_slug,
)
from app.shared.dependencies.pagination import Page, PageParams
from app.shared.exceptions import Conflict, ErrorCode, NotFound, ValidationFailed
from app.shared.schemas import image_urls
from app.shared.utils.search import build_boolean_query

logger = logging.getLogger(__name__)

RESERVED_PRODUCT_SLUGS = frozenset({"facets", "related"})
MAX_IMAGES_PER_PRODUCT = 15
MAX_FILES_PER_UPLOAD = 8
LAST_IMAGE_WARNING = "LAST_IMAGE_OF_ACTIVE_PRODUCT"

# Fields each type may NOT have (non-null).
FORBIDDEN_FIELDS: dict[ProductType, tuple[str, ...]] = {
    ProductType.simple: ("variants", "bundle_items"),
    ProductType.variable: ("price", "sale_price", "stock", "bundle_items"),
    ProductType.bundle: ("stock", "variants"),
}
REQUIRED_FIELDS: dict[ProductType, tuple[str, ...]] = {
    ProductType.simple: ("price", "stock"),
    ProductType.variable: (),
    ProductType.bundle: ("price",),
}


def _not_for_type(field: str, product_type: ProductType) -> ValidationFailed:
    return ValidationFailed(ErrorCode.INVALID_PRODUCT_FOR_TYPE, details={"field": field, "type": product_type.value})


def check_type_fields(product_type: ProductType, values: Mapping[str, Any]) -> None:
    for field in FORBIDDEN_FIELDS[product_type]:
        if values.get(field) is not None:
            raise _not_for_type(field, product_type)


def check_sale_price(price: Decimal | None, sale_price: Decimal | None) -> None:
    if sale_price is not None and (price is None or sale_price >= price):
        raise field_error("sale_price", "sale_price must be lower than price")


def _invalid_item(index: int, reason: str) -> ValidationFailed:
    return ValidationFailed(ErrorCode.INVALID_BUNDLE_ITEM, details={"index": index, "reason": reason})


class AdminProductService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ============================================================== loading helpers

    async def _product(self, product_id: int, *, for_update: bool = False) -> Product:
        stmt = select(Product).where(Product.id == product_id)
        if for_update:
            stmt = stmt.with_for_update()
        product = await self.session.scalar(stmt)
        if product is None:
            raise NotFound(ErrorCode.PRODUCT_NOT_FOUND)
        return product

    async def _category(self, category_id: int) -> Category:
        category = await self.session.get(Category, category_id)
        if category is None:
            raise field_error("category_id", "category not found", ErrorCode.CATEGORY_NOT_FOUND)
        return category

    async def _definitions(self, category: Category) -> EffectiveDefinitions:
        return await load_effective_definitions(self.session, category.id, category.parent_id)

    async def _check_brand(self, brand_id: int | None) -> None:
        if brand_id is not None and await self.session.get(Brand, brand_id) is None:
            raise field_error("brand_id", "brand not found", ErrorCode.BRAND_NOT_FOUND)

    async def _variants(self, product_id: int) -> list[ProductVariant]:
        result = await self.session.scalars(
            select(ProductVariant)
            .where(ProductVariant.product_id == product_id)
            .order_by(ProductVariant.sort_order, ProductVariant.id)
        )
        return list(result.all())

    async def _variant(self, product: Product, variant_id: int, *, for_update: bool = False) -> ProductVariant:
        stmt = select(ProductVariant).where(ProductVariant.id == variant_id, ProductVariant.product_id == product.id)
        if for_update:
            stmt = stmt.with_for_update()
        variant = await self.session.scalar(stmt)
        if variant is None:
            raise NotFound(ErrorCode.VARIANT_NOT_FOUND)
        return variant

    async def _product_sku(self, product: Product) -> str:
        base = f"LM-{product.id}"
        candidate, n = base, 1
        while await sku_taken(self.session, candidate, product_id=product.id):
            n += 1
            candidate = f"{base}-{n}"
        return candidate

    async def _variant_sku(self, product: Product, taken_locally: set[str]) -> str:
        count = int(
            await self.session.scalar(select(func.count(ProductVariant.id)).where(ProductVariant.product_id == product.id))
            or 0
        )
        n = count + 1
        while True:
            candidate = f"LM-{product.id}-{n}"
            if candidate not in taken_locally and not await sku_taken(self.session, candidate):
                return candidate
            n += 1

    # ======================================================================== create

    async def create(self, data: ProductCreate) -> AdminProductOut:
        values = data.model_dump()
        check_type_fields(data.type, values)
        for field in REQUIRED_FIELDS[data.type]:
            if values.get(field) is None:
                raise field_error(field, f"required for {data.type.value} products")
        check_sale_price(data.price, data.sale_price)

        category = await self._category(data.category_id)
        await self._check_brand(data.brand_id)
        definitions = await self._definitions(category)
        attributes = validate_attributes(definitions, data.attributes)
        slug = await unique_slug(
            self.session, Product, slug=data.slug, name=data.name, fallback_prefix="product",
            reserved=RESERVED_PRODUCT_SLUGS,
        )
        sku = normalize_sku(data.sku) if data.sku else None
        if sku:
            await ensure_sku_available(self.session, sku)

        cleaned_variants = [validate_variant_options(definitions, v.options) for v in data.variants or []]
        validate_variant_set(cleaned_variants)
        for v in data.variants or []:
            check_sale_price(v.price, v.sale_price)

        product = Product(
            type=data.type, name=data.name, slug=slug, description=data.description, category_id=category.id,
            brand_id=data.brand_id, sku=sku, price=data.price, sale_price=data.sale_price, stock=data.stock,
            attributes=attributes, is_new=data.is_new, is_active=False, search_text="",
        )
        self.session.add(product)
        await flush_catalog(self.session)
        if product.sku is None and data.type != ProductType.variable:
            product.sku = await self._product_sku(product)

        if data.variants:
            await self._add_variants(product, data.variants, cleaned_variants)
        if data.bundle_items is not None:
            await self._replace_bundle_items(product, data.bundle_items)
        await flush_catalog(self.session)
        await refresh_products(self.session, [product.id])
        await self.session.commit()
        return await self.get(product.id)

    async def _add_variants(
        self, product: Product, variants: Sequence[VariantIn], cleaned: Sequence[dict[str, str]]
    ) -> list[ProductVariant]:
        created: list[ProductVariant] = []
        local_skus: set[str] = set()
        for data, options in zip(variants, cleaned, strict=True):
            if data.sku:
                sku = normalize_sku(data.sku)
                if sku in local_skus:
                    raise field_error("sku", "duplicate sku in request", ErrorCode.SKU_TAKEN)
                await ensure_sku_available(self.session, sku)
            else:
                sku = await self._variant_sku(product, local_skus)
            local_skus.add(sku)
            variant = ProductVariant(
                product_id=product.id, sku=sku, options=options, options_signature=build_options_signature(options),
                price=data.price, sale_price=data.sale_price, stock=data.stock, is_active=data.is_active,
                sort_order=data.sort_order,
            )
            self.session.add(variant)
            created.append(variant)
            await flush_catalog(self.session)
        return created

    # ======================================================================== update

    async def update(self, product_id: int, data: ProductUpdate) -> AdminProductOut:
        product = await self._product(product_id, for_update=True)
        changes = data.model_dump(exclude_unset=True)
        if changes.get("type") is not None and changes["type"] != product.type:
            raise ValidationFailed(
                ErrorCode.PRODUCT_TYPE_IMMUTABLE, details={"type": product.type.value, "requested": changes["type"].value}
            )
        changes.pop("type", None)
        check_type_fields(product.type, changes)
        for field in REQUIRED_FIELDS[product.type]:
            if field in changes and changes[field] is None:
                raise field_error(field, f"required for {product.type.value} products")
        for field in ("name", "is_new", "is_active", "category_id"):
            if field in changes and changes[field] is None:
                changes.pop(field)

        price = changes.get("price", product.price)
        sale_price = changes.get("sale_price", product.sale_price) if "sale_price" in changes else product.sale_price
        check_sale_price(price, sale_price)

        # Validate everything before changing anything.
        category = await self._category(changes.get("category_id", product.category_id))
        if "brand_id" in changes:
            await self._check_brand(changes["brand_id"])
        category_changed = category.id != product.category_id
        definitions = await self._definitions(category)
        attributes = product.attributes
        if "attributes" in changes or category_changed:
            attributes = validate_attributes(
                definitions, changes["attributes"] if "attributes" in changes else product.attributes
            )
        if category_changed and product.type == ProductType.variable:
            cleaned = [validate_variant_options(definitions, v.options) for v in await self._variants(product.id)]
            validate_variant_set(cleaned)

        if "slug" in changes and changes["slug"] is not None and changes["slug"] != product.slug:
            product.slug = await unique_slug(
                self.session, Product, slug=changes["slug"], name=product.name, fallback_prefix="product",
                exclude_id=product.id, reserved=RESERVED_PRODUCT_SLUGS,
            )
        if "sku" in changes:
            if changes["sku"] is None:
                product.sku = None if product.type == ProductType.variable else await self._product_sku(product)
            else:
                sku = normalize_sku(changes["sku"])
                if sku != product.sku:
                    await ensure_sku_available(self.session, sku, product_id=product.id)
                product.sku = sku

        for field in ("name", "description", "brand_id", "price", "sale_price", "stock", "is_new"):
            if field in changes:
                setattr(product, field, changes[field])
        product.category_id = category.id
        product.attributes = attributes

        if "is_active" in changes:
            if changes["is_active"] and not product.is_active:
                await flush_catalog(self.session)
                reasons = await self._publish_reasons(product, category, definitions)
                if reasons:
                    raise ValidationFailed(ErrorCode.PRODUCT_NOT_PUBLISHABLE, details={"reasons": reasons})
            product.is_active = changes["is_active"]

        await flush_catalog(self.session)
        await refresh_products(self.session, [product.id])
        await self.session.commit()
        return await self.get(product.id)

    async def _publish_reasons(
        self, product: Product, category: Category, definitions: EffectiveDefinitions
    ) -> list[str]:
        reasons: list[str] = []
        images = await self.session.scalar(select(func.count(ProductImage.id)).where(ProductImage.product_id == product.id))
        if not images:
            reasons.append("NO_IMAGES")
        if missing_required(definitions, product.attributes or {}):
            reasons.append("MISSING_REQUIRED_ATTRIBUTE")
        if product.type == ProductType.variable:
            active = await self.session.scalar(
                select(func.count(ProductVariant.id)).where(
                    ProductVariant.product_id == product.id, ProductVariant.is_active.is_(True)
                )
            )
            if not active:
                reasons.append("NO_ACTIVE_VARIANTS")
        if product.type == ProductType.bundle:
            items = await self.session.scalar(
                select(func.count(BundleItem.id)).where(BundleItem.bundle_product_id == product.id)
            )
            if not items:
                reasons.append("NO_BUNDLE_ITEMS")
        parent = await self.session.get(Category, category.parent_id) if category.parent_id else None
        if not category.is_active or (parent is not None and not parent.is_active):
            reasons.append("CATEGORY_INACTIVE")
        if product.brand_id is not None:
            brand = await self.session.get(Brand, product.brand_id)
            if brand is not None and not brand.is_active:
                reasons.append("BRAND_INACTIVE")
        return reasons

    # ======================================================================== delete

    async def delete(self, product_id: int) -> None:
        product = await self._product(product_id, for_update=True)
        if await self.session.scalar(select(exists().where(OrderItem.product_id == product.id))):
            raise Conflict(ErrorCode.PRODUCT_HAS_ORDERS)
        if await self.session.scalar(select(exists().where(BundleItem.product_id == product.id))):
            raise Conflict(ErrorCode.PRODUCT_IN_BUNDLE)
        keys = (await self.session.scalars(select(ProductImage.image_key).where(ProductImage.product_id == product.id))).all()
        media = MediaTransaction(self.session)
        for key in keys:
            media.delete_after_commit(key)
        self.session.expunge(product)
        await self.session.execute(delete(Product).where(Product.id == product_id))
        await media.commit()

    # ========================================================================= stock

    async def adjust_stock(self, product_id: int, delta: int) -> StockOut:
        product = await self._product(product_id, for_update=True)
        if product.type != ProductType.simple:
            raise _not_for_type("stock", product.type)
        result = await self.session.execute(
            update(Product)
            .where(Product.id == product.id, Product.stock + delta >= 0)
            .values(stock=Product.stock + delta)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise ValidationFailed(ErrorCode.STOCK_NEGATIVE, details={"stock": product.stock, "delta": delta})
        stock = int(await self.session.scalar(select(Product.stock).where(Product.id == product.id)) or 0)
        await refresh_products(self.session, [product.id])
        await self.session.commit()
        return StockOut(stock=stock)

    # ====================================================================== variants

    async def _variable(self, product_id: int) -> Product:
        product = await self._product(product_id, for_update=True)
        if product.type != ProductType.variable:
            raise _not_for_type("variants", product.type)
        return product

    async def add_variant(self, product_id: int, data: VariantIn) -> AdminProductOut:
        product = await self._variable(product_id)
        definitions = await self._definitions(await self._category(product.category_id))
        options = validate_variant_options(definitions, data.options)
        validate_variant_set([*(v.options for v in await self._variants(product.id)), options])
        check_sale_price(data.price, data.sale_price)
        await self._add_variants(product, [data], [options])
        await refresh_products(self.session, [product.id])
        await self.session.commit()
        return await self.get(product.id)

    async def update_variant(self, product_id: int, variant_id: int, data: VariantUpdate) -> AdminProductOut:
        product = await self._variable(product_id)
        variant = await self._variant(product, variant_id, for_update=True)
        changes = data.model_dump(exclude_unset=True)
        for field in ("price", "stock", "is_active", "sort_order", "options"):
            if field in changes and changes[field] is None:
                changes.pop(field)
        if "options" in changes:
            definitions = await self._definitions(await self._category(product.category_id))
            options = validate_variant_options(definitions, changes["options"])
            others = [v.options for v in await self._variants(product.id) if v.id != variant.id]
            validate_variant_set([*others, options])
            variant.options = options
            variant.options_signature = build_options_signature(options)
        price = changes.get("price", variant.price)
        sale_price = changes["sale_price"] if "sale_price" in changes else variant.sale_price
        check_sale_price(price, sale_price)
        if "sku" in changes:
            sku = normalize_sku(changes["sku"]) if changes["sku"] else await self._variant_sku(product, set())
            if sku != variant.sku:
                await ensure_sku_available(self.session, sku, variant_id=variant.id)
            variant.sku = sku
        for field in ("price", "sale_price", "stock", "is_active", "sort_order"):
            if field in changes:
                setattr(variant, field, changes[field])
        await flush_catalog(self.session)
        await refresh_products(self.session, [product.id])
        await self.session.commit()
        return await self.get(product.id)

    async def delete_variant(self, product_id: int, variant_id: int) -> AdminProductOut:
        product = await self._variable(product_id)
        variant = await self._variant(product, variant_id, for_update=True)
        if await self.session.scalar(select(exists().where(OrderItem.variant_id == variant.id))):
            raise Conflict(ErrorCode.VARIANT_HAS_ORDERS)
        if await self.session.scalar(select(exists().where(BundleItem.variant_id == variant.id))):
            raise Conflict(ErrorCode.VARIANT_IN_BUNDLE)
        await self.session.delete(variant)
        await self.session.flush()
        await refresh_products(self.session, [product.id])
        await self.session.commit()
        return await self.get(product.id)

    async def adjust_variant_stock(self, product_id: int, variant_id: int, delta: int) -> StockOut:
        product = await self._variable(product_id)
        variant = await self._variant(product, variant_id, for_update=True)
        result = await self.session.execute(
            update(ProductVariant)
            .where(ProductVariant.id == variant.id, ProductVariant.stock + delta >= 0)
            .values(stock=ProductVariant.stock + delta)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise ValidationFailed(ErrorCode.STOCK_NEGATIVE, details={"stock": variant.stock, "delta": delta})
        stock = int(await self.session.scalar(select(ProductVariant.stock).where(ProductVariant.id == variant.id)) or 0)
        await refresh_products(self.session, [product.id])
        await self.session.commit()
        return StockOut(stock=stock)

    # ======================================================================= bundles

    async def replace_bundle_items(self, product_id: int, items: Sequence[BundleItemIn]) -> AdminProductOut:
        product = await self._product(product_id, for_update=True)
        if product.type != ProductType.bundle:
            raise _not_for_type("bundle_items", product.type)
        await self._replace_bundle_items(product, items)
        await refresh_products(self.session, [product.id])
        await self.session.commit()
        return await self.get(product.id)

    async def _replace_bundle_items(self, product: Product, items: Sequence[BundleItemIn]) -> None:
        """Validate all items, then replace the bundle's contents atomically."""
        pairs = [(i.product_id, i.variant_id) for i in items]
        for index, pair in enumerate(pairs):
            if pairs.index(pair) != index:
                raise _invalid_item(index, "duplicate product/variant in the bundle")
        product_ids = {i.product_id for i in items}
        variant_ids = {i.variant_id for i in items if i.variant_id is not None}
        components: dict[int, Product] = {}
        if product_ids:
            found = await self.session.scalars(select(Product).where(Product.id.in_(product_ids)))
            components = {p.id: p for p in found.all()}
        variant_owner: dict[int, int] = {}
        if variant_ids:
            rows = await self.session.execute(
                select(ProductVariant.id, ProductVariant.product_id).where(ProductVariant.id.in_(variant_ids))
            )
            variant_owner = {variant_id: owner_id for variant_id, owner_id in rows.all()}
        for index, item in enumerate(items):
            component = components.get(item.product_id)
            if component is None:
                raise _invalid_item(index, "product not found")
            if component.id == product.id:
                raise _invalid_item(index, "a bundle cannot contain itself")
            if component.type == ProductType.bundle:
                raise _invalid_item(index, "a bundle cannot contain another bundle")
            if component.type == ProductType.variable:
                if item.variant_id is None:
                    raise _invalid_item(index, "variant_id is required for a variable product")
                if variant_owner.get(item.variant_id) != component.id:
                    raise _invalid_item(index, "variant does not belong to the product")
            elif item.variant_id is not None:
                raise _invalid_item(index, "a simple product has no variants")

        await self.session.execute(delete(BundleItem).where(BundleItem.bundle_product_id == product.id))
        for item in items:
            self.session.add(
                BundleItem(
                    bundle_product_id=product.id, product_id=item.product_id, variant_id=item.variant_id,
                    quantity=item.quantity,
                )
            )
        await flush_catalog(self.session)

    # ======================================================================== images

    async def upload_images(
        self, product_id: int, files: Sequence[bytes], variant_id: int | None, color: str | None
    ) -> ImagesUploadOut:
        product = await self._product(product_id, for_update=True)
        if not 1 <= len(files) <= MAX_FILES_PER_UPLOAD:
            raise field_error("files", f"send 1..{MAX_FILES_PER_UPLOAD} files")
        if variant_id is not None and color is not None:
            raise field_error("color", "send either variant_id or color, not both")
        existing = int(
            await self.session.scalar(select(func.count(ProductImage.id)).where(ProductImage.product_id == product.id))
            or 0
        )
        if existing + len(files) > MAX_IMAGES_PER_PRODUCT:
            raise ValidationFailed(
                ErrorCode.IMAGE_LIMIT_REACHED, details={"max": MAX_IMAGES_PER_PRODUCT, "existing": existing}
            )
        variant, resolved_color = await self._image_target(product, variant_id, color)
        next_order = int(
            await self.session.scalar(
                select(func.coalesce(func.max(ProductImage.sort_order), -1)).where(ProductImage.product_id == product.id)
            )
            or 0
        ) + 1

        media = MediaTransaction(self.session)
        created: list[ProductImage] = []
        try:
            for offset, data in enumerate(files):
                saved = await media.save_image(f"products/{product.id}", data)
                image = ProductImage(
                    product_id=product.id, variant_id=variant.id if variant else None, image_key=saved.image_key,
                    width=saved.width, height=saved.height, sort_order=next_order + offset,
                )
                self.session.add(image)
                created.append(image)
            await self.session.flush()
        except BaseException:
            await media.discard()
            raise
        await media.commit()
        colors = {i.id: resolved_color for i in created}
        return ImagesUploadOut(images=[self._image_out(i, colors) for i in created], color=resolved_color)

    async def _image_target(
        self, product: Product, variant_id: int | None, color: str | None
    ) -> tuple[ProductVariant | None, str | None]:
        if variant_id is None and color is None:
            return None, None
        if product.type != ProductType.variable:
            raise _not_for_type("variant_id" if variant_id is not None else "color", product.type)
        definitions = await self._definitions(await self._category(product.category_id))
        color_def = definitions.color_option
        if variant_id is not None:
            variant = await self._variant(product, variant_id)
            return variant, variant.options.get(color_def.key) if color_def else None
        variants = [v for v in await self._variants(product.id) if v.is_active]
        match = next(
            (v for v in variants if color_def is not None and v.options.get(color_def.key) == color), None
        )
        if match is None:
            raise ValidationFailed(ErrorCode.VARIANT_NOT_FOUND, details={"color": color})
        return match, color

    async def update_image(self, product_id: int, image_id: int, data: ImageUpdate) -> AdminImageOut:
        product = await self._product(product_id)
        image = await self._image(product, image_id)
        changes = data.model_dump(exclude_unset=True)
        if "variant_id" in changes and changes["variant_id"] is not None:
            await self._variant(product, changes["variant_id"])
        if "alt_text" in changes:
            image.alt_text = changes["alt_text"] or None
        if "variant_id" in changes:
            image.variant_id = changes["variant_id"]
        await self.session.commit()
        colors = await self._image_colors(product, [image])
        return self._image_out(image, colors)

    async def reorder_images(self, product_id: int, image_ids: Sequence[int]) -> list[AdminImageOut]:
        product = await self._product(product_id, for_update=True)
        images = list(
            (await self.session.scalars(select(ProductImage).where(ProductImage.product_id == product.id))).all()
        )
        if len(image_ids) != len(set(image_ids)) or set(image_ids) != {i.id for i in images}:
            raise ValidationFailed(
                ErrorCode.IMAGE_ORDER_MISMATCH, details={"expected": sorted(i.id for i in images)}
            )
        by_id = {i.id: i for i in images}
        for index, image_id in enumerate(image_ids):
            by_id[image_id].sort_order = index
        await self.session.commit()
        ordered = [by_id[i] for i in image_ids]
        colors = await self._image_colors(product, ordered)
        return [self._image_out(i, colors) for i in ordered]

    async def delete_image(self, product_id: int, image_id: int) -> ImageDeleteOut:
        product = await self._product(product_id, for_update=True)
        image = await self._image(product, image_id)
        media = MediaTransaction(self.session)
        media.delete_after_commit(image.image_key)
        await self.session.delete(image)
        await self.session.flush()
        remaining = int(
            await self.session.scalar(select(func.count(ProductImage.id)).where(ProductImage.product_id == product.id))
            or 0
        )
        await media.commit()
        warning = LAST_IMAGE_WARNING if product.is_active and remaining == 0 else None
        return ImageDeleteOut(deleted=True, warning=warning)

    async def _image(self, product: Product, image_id: int) -> ProductImage:
        image = await self.session.scalar(
            select(ProductImage).where(ProductImage.id == image_id, ProductImage.product_id == product.id)
        )
        if image is None:
            raise NotFound(ErrorCode.IMAGE_NOT_FOUND)
        return image

    async def _image_colors(self, product: Product, images: Sequence[ProductImage]) -> dict[int, str | None]:
        if product.type != ProductType.variable:
            return {i.id: None for i in images}
        definitions = await self._definitions(await self._category(product.category_id))
        variants = (await repo.variants_of(self.session, [product.id], active_only=False)).get(product.id, [])
        loaded = [repo_image(i) for i in images]
        return image_colors(loaded, variants, definitions.color_option)

    @staticmethod
    def _image_out(image: ProductImage, colors: Mapping[int, str | None]) -> AdminImageOut:
        return AdminImageOut(
            id=image.id, image_key=image.image_key, variant_id=image.variant_id, color=colors.get(image.id),
            alt_text=image.alt_text, width=image.width, height=image.height, sort_order=image.sort_order,
            urls=image_urls(image.image_key),  # type: ignore[arg-type]
        )

    # ========================================================================== read

    async def get(self, product_id: int) -> AdminProductOut:
        product = await self._product(product_id)
        category = await self._category(product.category_id)
        brand = await self.session.get(Brand, product.brand_id) if product.brand_id else None
        definitions = await self._definitions(category)
        variants = await self._variants(product.id)
        images = list(
            (
                await self.session.scalars(
                    select(ProductImage)
                    .where(ProductImage.product_id == product.id)
                    .order_by(ProductImage.sort_order, ProductImage.id)
                )
            ).all()
        )
        loaded_variants = [repo.loaded_variant(v) for v in variants]
        loaded_images = [repo_image(i) for i in images]
        color = definitions.color_option
        colors = image_colors(loaded_images, loaded_variants, color)
        components = await repo.bundle_components(self.session, product.id) if product.type == ProductType.bundle else []

        warnings: list[ProductWarning] = []
        missing_colors = colors_without_images(loaded_images, loaded_variants, color)
        if missing_colors:
            warnings.append(ProductWarning(code="COLOR_WITHOUT_IMAGES", colors=missing_colors))
        reasons = await self._publish_reasons(product, category, definitions)

        return AdminProductOut(
            id=product.id, type=product.type, name=product.name, slug=product.slug, description=product.description,
            category=CategoryRef(id=category.id, name=category.name, slug=category.slug),
            brand=BrandRef(id=brand.id, name=brand.name, slug=brand.slug) if brand else None,
            sku=product.sku, price=product.price, sale_price=product.sale_price, stock=product.stock,
            bundle_stock=bundle_stock([BundleComponent(c.quantity, c.stock, c.active) for c in components])
            if product.type == ProductType.bundle
            else None,
            attributes=product.attributes or {}, price_from=product.price_from, in_stock=product.in_stock,
            is_on_sale=product.is_on_sale, is_new=product.is_new, is_active=product.is_active,
            created_at=product.created_at, updated_at=product.updated_at,
            variants=[
                AdminVariantOut(
                    id=v.id, sku=v.sku, options=v.options, options_signature=v.options_signature,
                    color=v.options.get(color.key) if color else None, price=v.price, sale_price=v.sale_price,
                    current_price=v.sale_price if v.sale_price is not None else v.price, stock=v.stock,
                    is_active=v.is_active, sort_order=v.sort_order, created_at=v.created_at, updated_at=v.updated_at,
                )
                for v in variants
            ],
            images=[self._image_out(i, colors) for i in images],
            bundle_items=[
                AdminBundleItemOut(
                    id=c.item_id, product_id=c.product_id, product_name=c.product_name, product_slug=c.product_slug,
                    variant_id=c.variant_id,
                    variant_label=variant_label(c.variant_options) if c.variant_options else None,
                    quantity=c.quantity, component_stock=c.stock, component_active=c.active,
                )
                for c in components
            ],
            publishable=Publishable(ok=not reasons, reasons=reasons),
            warnings=warnings,
        )

    async def list(
        self,
        params: PageParams,
        *,
        q: str | None,
        category_id: int | None,
        brand_id: int | None,
        product_type: ProductType | None,
        is_active: bool | None,
        in_stock: bool | None,
        low_stock: bool,
        sort: AdminProductSort,
    ) -> Page[AdminProductListItem]:
        stock = repo.stock_expression()
        image = (
            select(ProductImage.image_key)
            .where(ProductImage.product_id == Product.id)
            .order_by(ProductImage.sort_order, ProductImage.id)
            .limit(1)
            .correlate(Product)
            .scalar_subquery()
        )
        conditions: list[Any] = []
        if q and q.strip():
            boolean = build_boolean_query(q)
            sku_match = Product.sku == q.strip().upper()
            variant_sku = exists().where(ProductVariant.product_id == Product.id, ProductVariant.sku == q.strip().upper())
            if boolean:
                conditions.append(or_(search_condition(boolean), sku_match, variant_sku))
            else:
                conditions.append(or_(sku_match, variant_sku))
        if category_id is not None:
            sub = select(Category.id).where(or_(Category.id == category_id, Category.parent_id == category_id))
            conditions.append(Product.category_id.in_(sub))
        if brand_id is not None:
            conditions.append(Product.brand_id == brand_id)
        if product_type is not None:
            conditions.append(Product.type == product_type)
        if is_active is not None:
            conditions.append(Product.is_active.is_(is_active))
        if in_stock is not None:
            conditions.append(Product.in_stock.is_(in_stock))
        if low_stock:
            threshold = await get_low_stock_threshold(self.session)
            conditions.append(
                or_(
                    and_(Product.type == ProductType.simple.value, Product.stock <= threshold),
                    exists().where(
                        ProductVariant.product_id == Product.id,
                        ProductVariant.is_active.is_(True),
                        ProductVariant.stock <= threshold,
                    ),
                )
            )
        order = {
            AdminProductSort.newest: [Product.created_at.desc(), Product.id.desc()],
            AdminProductSort.name: [Product.name.asc(), Product.id.asc()],
            AdminProductSort.price_asc: [Product.price_from.asc(), Product.id.desc()],
            AdminProductSort.price_desc: [Product.price_from.desc(), Product.id.desc()],
            AdminProductSort.stock_asc: [stock.asc(), Product.id.desc()],
        }[sort]
        rows = (
            await self.session.execute(
                select(
                    *repo.CARD_COLUMNS, Product.sku, Product.is_active, Product.created_at, Product.updated_at,
                    stock.label("stock_value"), image.label("image_key"), func.count().over().label("total"),
                )
                .join(Category, Category.id == Product.category_id)
                .outerjoin(Brand, Brand.id == Product.brand_id)
                .where(*conditions)
                .order_by(*order)
                .offset(params.offset)
                .limit(params.page_size)
            )
        ).all()
        if rows:
            total = int(rows[0].total)
        else:
            total = int(await self.session.scalar(select(func.count(Product.id)).where(*conditions)) or 0)
        items = []
        for r in rows:
            card = repo.card_row(r)
            items.append(
                AdminProductListItem(
                    id=r.id, type=r.type, name=r.name, slug=r.slug, sku=r.sku, category=card.category, brand=card.brand,
                    image=image_urls(r.image_key), price_from=r.price_from,
                    stock=int(r.stock_value) if r.stock_value is not None else None, in_stock=r.in_stock,
                    is_on_sale=r.is_on_sale, is_new=r.is_new, is_active=r.is_active, created_at=r.created_at,
                    updated_at=r.updated_at,
                )
            )
        return Page[AdminProductListItem](items=items, page=params.page, page_size=params.page_size, total=total)

    async def low_stock(self, params: PageParams, threshold: int | None) -> Page[LowStockItem]:
        """Active simple products and active variants with stock <= threshold, lowest first."""
        limit = threshold if threshold is not None else await get_low_stock_threshold(self.session)
        # Variants first so the UNION's column types (JSON options) come from typed columns.
        variants = (
            select(
                Product.id.label("product_id"), Product.name.label("product_name"),
                Product.slug.label("product_slug"), ProductVariant.id.label("variant_id"),
                ProductVariant.options.label("options"), ProductVariant.sku.label("sku"),
                ProductVariant.stock.label("stock"),
            )
            .join(Product, Product.id == ProductVariant.product_id)
            .where(Product.is_active.is_(True), ProductVariant.is_active.is_(True), ProductVariant.stock <= limit)
        )
        simple = select(
            Product.id, Product.name, Product.slug, null(), null(), Product.sku, Product.stock
        ).where(Product.type == ProductType.simple.value, Product.is_active.is_(True), Product.stock <= limit)
        sub = union_all(variants, simple).subquery()
        rows = (
            await self.session.execute(
                select(sub, func.count().over().label("total"))
                .order_by(sub.c.stock.asc(), sub.c.product_name.asc(), sub.c.variant_id.asc())
                .offset(params.offset)
                .limit(params.page_size)
            )
        ).all()
        total = int(rows[0].total) if rows else int(await self.session.scalar(select(func.count()).select_from(sub)) or 0)
        return Page[LowStockItem](
            items=[
                LowStockItem(
                    product_id=r.product_id, product_name=r.product_name, product_slug=r.product_slug,
                    variant_id=r.variant_id, variant_label=variant_label(_as_dict(r.options)) if r.options else None,
                    sku=r.sku, stock=r.stock,
                )
                for r in rows
            ],
            page=params.page,
            page_size=params.page_size,
            total=total,
        )


def repo_image(image: ProductImage) -> LoadedImage:
    return LoadedImage(
        image.id, image.variant_id, image.image_key, image.alt_text, image.width, image.height, image.sort_order
    )


def _as_dict(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value)
