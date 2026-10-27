"""Public catalog reads: product list, facets, detail, related. Read-only, bounded queries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.brands.schemas import BrandRef
from app.modules.categories.definitions import load_definition_rows, merge_definitions
from app.modules.categories.enums import AttributeType
from app.modules.categories.models import Category
from app.modules.categories.schemas import CategoryRef
from app.modules.categories.tree import CategoryIndex, load_category_index
from app.modules.favorites.repository import favorite_ids_among
from app.modules.products import repository as repo
from app.modules.products.enums import ProductType
from app.modules.products.filters import (
    FilterDefinitions,
    ProductFilters,
    definitions_for_all,
    definitions_for_category,
    has_definition_params,
    parse_definition_filters,
    validate_price_range,
)
from app.modules.products.presenters import (
    CardRow,
    attribute_values,
    build_card,
    image_colors,
    price_info,
    stock_status,
    variant_image_ids,
    variant_option_values,
)
from app.modules.products.schemas import (
    AttributeFacet,
    BrandFacet,
    BundleItemOut,
    BundleItemProductOut,
    FacetsOut,
    FacetValue,
    OptionFacet,
    PriceRange,
    ProductCardOut,
    ProductDetailOut,
    ProductImageOut,
    ProductSort,
    VariantOut,
)
from app.modules.products.validation import variant_label
from app.modules.settings.service import hide_out_of_stock, parse_int_setting
from app.shared.dependencies.pagination import Page, PageParams
from app.shared.exceptions import ErrorCode, NotFound
from app.shared.schemas import image_urls
from app.shared.utils.search import build_boolean_query

FACET_ATTRIBUTE_TYPES = {AttributeType.select, AttributeType.multiselect, AttributeType.boolean, AttributeType.number}


@dataclass(frozen=True)
class ListQuery:
    category: str | None
    brands: list[str]
    q: str | None
    min_price: Decimal | None
    max_price: Decimal | None
    in_stock: bool
    on_sale: bool
    is_new: bool
    params: list[tuple[str, str]]


@dataclass(frozen=True)
class ResolvedFilters:
    filters: ProductFilters
    category: Category | None
    index: CategoryIndex
    empty: bool  # q given but nothing searchable remained
    definitions: FilterDefinitions | None


class CatalogProductService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------- filters

    async def _resolve(self, query: ListQuery, *, need_definitions: bool = False) -> ResolvedFilters:
        validate_price_range(query.min_price, query.max_price)
        index = await load_category_index(self.session)
        category: Category | None = None
        filters = ProductFilters(
            brand_slugs=list(dict.fromkeys(query.brands)), min_price=query.min_price, max_price=query.max_price,
            in_stock=query.in_stock, hide_out_of_stock=await hide_out_of_stock(self.session),
            on_sale=query.on_sale, is_new=query.is_new,
        )
        if query.category is not None:
            category = index.by_slug(query.category)
            if category is None or not index.is_visible(category):
                raise NotFound(ErrorCode.CATEGORY_NOT_FOUND)
            filters.category_ids = index.subtree_ids(category)

        definitions: FilterDefinitions | None = None
        if has_definition_params(query.params) or (need_definitions and category is not None):
            if category is not None:
                parent = index.parent(category)
                rows = await load_definition_rows(self.session, [category.id, *([parent.id] if parent else [])])
                definitions = definitions_for_category(merge_definitions(category.id, category.parent_id, rows))
            else:
                definitions = definitions_for_all(await load_definition_rows(self.session, None))
            filters.attrs, filters.opts = parse_definition_filters(query.params, definitions)

        empty = False
        if query.q is not None and query.q.strip():
            filters.search = build_boolean_query(query.q)
            empty = filters.search is None
        return ResolvedFilters(filters=filters, category=category, index=index, empty=empty, definitions=definitions)

    # ---------------------------------------------------------------------- list

    async def _favorites(self, user_id: int | None, product_ids: Sequence[int]) -> set[int]:
        """At most ONE extra query, and only for a logged-in user."""
        if user_id is None:
            return set()
        return await favorite_ids_among(self.session, user_id, product_ids)

    async def list(
        self, query: ListQuery, sort: ProductSort | None, params: PageParams, user_id: int | None = None
    ) -> Page[ProductCardOut]:
        resolved = await self._resolve(query)
        if resolved.empty:
            return Page[ProductCardOut](items=[], page=params.page, page_size=params.page_size, total=0)
        filters = resolved.filters
        effective_sort = sort or (ProductSort.relevance if filters.search else ProductSort.newest)
        rows, total = await repo.list_cards(self.session, filters, effective_sort, params.offset, params.page_size)
        if total is None:
            total = await repo.count_products(self.session, filters) if params.page > 1 else 0
        favorites = await self._favorites(user_id, [r.id for r in rows])
        return Page[ProductCardOut](
            items=await self.cards(rows, favorites), page=params.page, page_size=params.page_size, total=total
        )

    async def by_ids(self, ids: Sequence[int], sort: ProductSort | None, params: PageParams) -> Page[ProductCardOut]:
        """GET /products?ids=: visible products among the ids (out-of-stock ones included), in the
        order of the ids unless a sort is given. Other filters do not apply."""
        rows, total = await repo.cards_by_ids(self.session, ids, sort, params.offset, params.page_size)
        if total is None:
            total = len(await repo.visible_ids_among(self.session, ids)) if params.page > 1 else 0
        return Page[ProductCardOut](
            items=await self.cards(rows), page=params.page, page_size=params.page_size, total=total
        )

    async def cards(self, rows: Sequence[CardRow], favorite_ids: set[int] | None = None) -> list[ProductCardOut]:
        """THE card builder (catalog lists, related, favorites): two batched queries (plus the
        catalog_hide_out_of_stock setting, once per catalog version and process)."""
        if not rows:
            return []
        ids = [r.id for r in rows]
        images = await repo.main_image_keys(self.session, ids)
        variable_ids = [r.id for r in rows if r.type == ProductType.variable]
        variants = await repo.variants_of(self.session, variable_ids, active_only=True)
        in_stock_swatches = bool(variable_ids) and await hide_out_of_stock(self.session)
        cards = [
            build_card(r, images.get(r.id), variants.get(r.id, []), in_stock_swatches=in_stock_swatches) for r in rows
        ]
        for card in cards:
            card.is_favorite = card.id in (favorite_ids or set())
        return cards

    # -------------------------------------------------------------------- facets

    async def facets(self, query: ListQuery) -> FacetsOut:
        resolved = await self._resolve(query, need_definitions=True)
        if resolved.empty:
            return FacetsOut(brands=[], price=PriceRange(min=None, max=None))
        filters = resolved.filters
        brands = [BrandFacet(slug=s, name=n, count=c) for s, n, c in await repo.brand_facet(self.session, filters)]
        low, high = await repo.price_facet(self.session, filters)
        result = FacetsOut(brands=brands, price=PriceRange(min=low, max=high))
        definitions = resolved.definitions
        if resolved.category is None or definitions is None:
            return result

        attr_defs = [d for d in definitions.attributes.values() if d.type in FACET_ATTRIBUTE_TYPES]
        attr_rows = await repo.attribute_facets(self.session, filters, attr_defs)
        selected_attr = {f.key: set(f.values) for f in filters.attrs}
        for d in attr_defs:
            rows = [r for r in attr_rows if r.key == d.key]
            if d.type == AttributeType.number:
                row = rows[0] if rows else None
                result.attributes.append(
                    AttributeFacet(
                        key=d.key, label=d.label, type=d.type, unit=d.unit,
                        min=float(row.min) if row and row.min is not None else None,
                        max=float(row.max) if row and row.max is not None else None,
                    )
                )
                continue
            counts = {r.value: r.count for r in rows if r.value is not None}
            order = d.options if d.type != AttributeType.boolean else ["true", "false"]
            values = [
                FacetValue(value=v, count=counts.get(v, 0))
                for v in (order or sorted(counts))
                if counts.get(v, 0) > 0 or v in selected_attr.get(d.key, set())
            ]
            result.attributes.append(AttributeFacet(key=d.key, label=d.label, type=d.type, unit=d.unit, values=values))

        option_defs = list(definitions.options.values())
        option_rows = await repo.option_facets(self.session, filters, option_defs)
        selected_opt = {f.key: set(f.values) for f in filters.opts}
        for d in option_defs:
            rows = [r for r in option_rows if r.key == d.key and r.value is not None]
            by_value = {r.value: r for r in rows}
            ordered = [v for v in (d.options or []) if v in by_value] + sorted(
                (v for v in by_value if v not in (d.options or [])), key=lambda v: (-by_value[v].count, v)
            )
            values = [FacetValue(value=v, count=by_value[v].count, hex=by_value[v].hex) for v in ordered if v is not None]
            values += [FacetValue(value=v, count=0) for v in selected_opt.get(d.key, set()) if v not in by_value]
            result.options.append(OptionFacet(key=d.key, label=d.label, type=d.type, values=values))
        return result

    # -------------------------------------------------------------------- detail

    async def detail(self, slug: str, user_id: int | None = None) -> ProductDetailOut:
        index = await load_category_index(self.session)
        row = await repo.visible_product_by_slug(self.session, slug)
        if row is None:
            raise NotFound(ErrorCode.PRODUCT_NOT_FOUND)
        product, brand = row.product, row.brand
        threshold = parse_int_setting(row.threshold_raw, "low_stock_threshold")
        category = index.by_id[product.category_id]
        parent = index.parent(category)
        rows = await load_definition_rows(self.session, [category.id, *([parent.id] if parent else [])])
        definitions = merge_definitions(category.id, category.parent_id, rows)
        images = await repo.images_of(self.session, product.id)

        all_variants = []
        components = []
        if product.type == ProductType.variable:
            all_variants = (await repo.variants_of(self.session, [product.id], active_only=False)).get(product.id, [])
        elif product.type == ProductType.bundle:
            components = await repo.bundle_components(self.session, product.id)
        active = [v for v in all_variants if v.is_active]

        card = build_card(
            CardRow(
                id=product.id, slug=product.slug, name=product.name, type=product.type, price=product.price,
                sale_price=product.sale_price, price_from=product.price_from, is_new=product.is_new,
                is_on_sale=product.is_on_sale, in_stock=product.in_stock,
                brand=BrandRef(id=brand.id, name=brand.name, slug=brand.slug) if brand else None,
                category=CategoryRef(id=category.id, name=category.name, slug=category.slug),
            ),
            images[0].image_key if images else None,
            active,
        )
        color_key = definitions.color_option.key if definitions.color_option else None
        colors = image_colors(images, all_variants, color_key)
        detail = ProductDetailOut(
            **card.model_dump(),
            description=product.description,
            breadcrumb=[CategoryRef(id=c.id, name=c.name, slug=c.slug) for c in index.breadcrumb(category)],
            attributes=attribute_values(definitions, product.attributes or {}),
            images=[
                ProductImageOut(
                    id=i.id, alt_text=i.alt_text, variant_id=i.variant_id, color=colors.get(i.id),
                    width=i.width, height=i.height, urls=image_urls(i.image_key),  # type: ignore[arg-type]
                )
                for i in images
            ],
        )
        detail.is_favorite = bool(await self._favorites(user_id, [product.id]))
        if product.type == ProductType.variable:
            detail.variant_options = variant_option_values(definitions, active)
            detail.variants = []
            for v in active:
                status, left = stock_status(v.stock, threshold)
                prices = price_info(v.price, v.sale_price)
                detail.variants.append(
                    VariantOut(
                        id=v.id, options=v.options, price=prices.regular, sale_price=v.sale_price,
                        current_price=prices.current, discount_percent=prices.discount_percent,
                        stock_status=status, stock_left=left,
                        image_ids=variant_image_ids(v, images, colors, color_key),
                    )
                )
            return detail

        stock = repo.derived_bundle_stock(components) if product.type == ProductType.bundle else product.stock or 0
        prices = price_info(product.price, product.sale_price)
        detail.regular_price = prices.regular
        detail.sale_price = product.sale_price
        detail.current_price = prices.current
        detail.stock_status, detail.stock_left = stock_status(stock, threshold)
        detail.items = [
            BundleItemOut(
                product=BundleItemProductOut(
                    id=c.product_id, slug=c.product_slug, name=c.product_name, image=image_urls(c.image_key)
                ),
                variant_label=variant_label(c.variant_options) if c.variant_options else None,
                quantity=c.quantity,
            )
            for c in components
        ]
        return detail

    # ------------------------------------------------------------------- related

    async def related(self, slug: str, limit: int, user_id: int | None = None) -> list[ProductCardOut]:
        index = await load_category_index(self.session)
        row = await repo.visible_product_by_slug(self.session, slug)
        if row is None:
            raise NotFound(ErrorCode.PRODUCT_NOT_FOUND)
        product = row.product
        category = index.by_id[product.category_id]
        rows = await repo.related_cards(self.session, [category.id], [product.id], limit)
        parent = index.parent(category)
        if len(rows) < limit and parent is not None:
            rows += await repo.related_cards(
                self.session, index.subtree_ids(parent), [product.id, *(r.id for r in rows)], limit - len(rows)
            )
        return await self.cards(rows, await self._favorites(user_id, [r.id for r in rows]))
