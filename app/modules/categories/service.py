"""Categories: public tree/detail, and admin management including attribute and variant-option
definitions. Each admin method is one transaction and commits itself."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.storage.transaction import MediaTransaction
from app.shared.events import commit_and_publish
from app.modules.categories import repository as repo
from app.modules.categories.definitions import (
    EffectiveDefinitions,
    load_definition_rows,
    load_effective_definitions,
    merge_definitions,
)
from app.modules.categories.enums import AttributeType, VariantOptionType
from app.modules.categories.models import Category, CategoryAttribute, CategoryVariantOption
from app.modules.categories.schemas import (
    AdminAttributeOut,
    AdminCategoryDetail,
    AdminCategoryNode,
    AdminVariantOptionOut,
    AttributeCreate,
    AttributeDefinitionOut,
    AttributeUpdate,
    CategoryCreate,
    CategoryDetailOut,
    CategoryNode,
    CategoryRef,
    CategoryUpdate,
    VariantOptionCreate,
    VariantOptionDefinitionOut,
    VariantOptionUpdate,
    dump_set,
)
from app.modules.categories.tree import CategoryIndex, load_category_index
from app.modules.products import usage
from app.modules.products.models import Product, ProductVariant
from app.modules.products.summary import refresh_search_text
from app.modules.products.validation import validate_attributes, validate_variant_options, validate_variant_set
from app.shared.catalog import field_error, flush_catalog, json_path, unique_slug
from app.shared.exceptions import AppError, Conflict, ErrorCode, NotFound, ValidationFailed
from app.shared.schemas import image_urls

SELECT_TYPES = {AttributeType.select, AttributeType.multiselect}


def _subtree_counts(index: CategoryIndex, counts: dict[int, int], category: Category, visible_only: bool) -> int:
    total = counts.get(category.id, 0)
    for child in index.children.get(category.id, []):
        if not visible_only or index.is_visible(child):
            total += counts.get(child.id, 0)
    return total


# =========================================================================== public


class CategoryCatalogService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def tree(self) -> list[CategoryNode]:
        index = await load_category_index(self.session)
        counts = await repo.product_counts_by_category(self.session, visible_only=True)
        return [self._node(index, counts, root) for root in index.roots() if index.is_visible(root)]

    def _node(self, index: CategoryIndex, counts: dict[int, int], category: Category) -> CategoryNode:
        children = [c for c in index.children.get(category.id, []) if index.is_visible(c)]
        return CategoryNode(
            id=category.id, name=category.name, slug=category.slug, description=category.description,
            image=image_urls(category.image_key),
            product_count=_subtree_counts(index, counts, category, visible_only=True),
            children=[self._node(index, counts, c) for c in children],
        )

    async def detail(self, slug: str) -> CategoryDetailOut:
        index = await load_category_index(self.session)
        category = index.by_slug(slug)
        if category is None or not index.is_visible(category):
            raise NotFound(ErrorCode.CATEGORY_NOT_FOUND)
        counts = await repo.product_counts_by_category(self.session, visible_only=True)
        definitions = await load_effective_definitions(self.session, category.id, category.parent_id)
        node = self._node(index, counts, category)
        return CategoryDetailOut(
            id=category.id, name=category.name, slug=category.slug, description=category.description,
            image=node.image, product_count=node.product_count,
            breadcrumb=[CategoryRef(id=c.id, name=c.name, slug=c.slug) for c in index.breadcrumb(category)],
            children=node.children,
            attributes=[
                AttributeDefinitionOut(key=d.key, label=d.label, type=d.type, unit=d.unit, options=d.options)
                for d in definitions.filterable_attributes
            ],
            variant_options=[
                VariantOptionDefinitionOut(key=d.key, label=d.label, type=d.type, options=d.options)
                for d in definitions.variant_options.values()
            ],
        )


# ============================================================================ admin


class AdminCategoryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ categories

    async def tree(self) -> list[AdminCategoryNode]:
        index = await load_category_index(self.session)
        counts = await repo.product_counts_by_category(self.session, visible_only=False)
        return [self._node(index, counts, root) for root in index.roots()]

    def _node(self, index: CategoryIndex, counts: dict[int, int], category: Category) -> AdminCategoryNode:
        return AdminCategoryNode(
            id=category.id, name=category.name, slug=category.slug, parent_id=category.parent_id,
            description=category.description, image=image_urls(category.image_key), sort_order=category.sort_order,
            is_active=category.is_active, product_count=_subtree_counts(index, counts, category, visible_only=False),
            created_at=category.created_at, updated_at=category.updated_at,
            children=[self._node(index, counts, c) for c in index.children.get(category.id, [])],
        )

    async def detail(self, category_id: int) -> AdminCategoryDetail:
        category = await self._get(category_id)
        index = await load_category_index(self.session)
        counts = await repo.product_counts_by_category(self.session, visible_only=False)
        own_attrs, own_opts = await repo.own_definitions(self.session, category.id)
        effective = await load_effective_definitions(self.session, category.id, category.parent_id)
        node = self._node(index, counts, index.by_id[category.id])
        return AdminCategoryDetail(
            **node.model_dump(),
            own_attributes=[AdminAttributeOut.model_validate(a) for a in own_attrs],
            own_variant_options=[AdminVariantOptionOut.model_validate(o) for o in own_opts],
            effective_attributes=[AdminAttributeOut.model_validate(d, from_attributes=True) for d in effective.attributes.values()],
            effective_variant_options=[
                AdminVariantOptionOut.model_validate(d, from_attributes=True) for d in effective.variant_options.values()
            ],
        )

    async def create(self, data: CategoryCreate) -> AdminCategoryDetail:
        if data.parent_id is not None:
            await self._check_parent(None, data.parent_id)
        slug = await unique_slug(self.session, Category, slug=data.slug, name=data.name, fallback_prefix="category")
        category = Category(
            name=data.name, slug=slug, parent_id=data.parent_id, description=data.description,
            sort_order=data.sort_order, is_active=data.is_active,
        )
        self.session.add(category)
        await flush_catalog(self.session)
        await commit_and_publish(self.session)
        return await self.detail(category.id)

    async def update(self, category_id: int, data: CategoryUpdate) -> AdminCategoryDetail:
        category = await self._get(category_id, for_update=True)
        changes = dump_set(data)
        renamed = False
        if "name" in changes and changes["name"] is not None and changes["name"] != category.name:
            category.name = changes["name"]
            renamed = True
        if "slug" in changes and changes["slug"] is not None and changes["slug"] != category.slug:
            category.slug = await unique_slug(
                self.session, Category, slug=changes["slug"], name=category.name, fallback_prefix="category",
                exclude_id=category.id,
            )
        if "parent_id" in changes and changes["parent_id"] != category.parent_id:
            new_parent = changes["parent_id"]
            if new_parent is not None:
                await self._check_parent(category, new_parent)
            await self._revalidate_products(category.id, new_parent)
            category.parent_id = new_parent
            renamed = True  # parent name is part of the products' search text
        for field in ("description", "sort_order", "is_active"):
            if field in changes and (changes[field] is not None or field == "description"):
                setattr(category, field, changes[field])
        await flush_catalog(self.session)
        if renamed:
            subtree = [category.id, *await repo.child_ids(self.session, category.id)]
            await refresh_search_text(self.session, await repo.product_ids_in(self.session, subtree))
        await commit_and_publish(self.session)
        return await self.detail(category.id)

    async def delete(self, category_id: int) -> None:
        category = await self._get(category_id, for_update=True)
        children = await repo.count_children(self.session, category.id)
        products = await repo.count_products(self.session, [category.id])
        if children or products:
            raise Conflict(ErrorCode.CATEGORY_NOT_EMPTY, details={"children": children, "products": products})
        media = MediaTransaction(self.session)
        media.delete_after_commit(category.image_key)
        await self.session.delete(category)
        await media.commit()

    async def set_image(self, category_id: int, data: bytes) -> AdminCategoryDetail:
        category = await self._get(category_id, for_update=True)
        media = MediaTransaction(self.session)
        saved = await media.save_image(f"categories/{category.id}", data)
        media.delete_after_commit(category.image_key)
        category.image_key = saved.image_key
        await media.commit()
        return await self.detail(category.id)

    async def delete_image(self, category_id: int) -> AdminCategoryDetail:
        category = await self._get(category_id, for_update=True)
        media = MediaTransaction(self.session)
        media.delete_after_commit(category.image_key)
        category.image_key = None
        await media.commit()
        return await self.detail(category.id)

    async def _get(self, category_id: int, *, for_update: bool = False) -> Category:
        category = await repo.get_category(self.session, category_id, for_update=for_update)
        if category is None:
            raise NotFound(ErrorCode.CATEGORY_NOT_FOUND)
        return category

    async def _check_parent(self, category: Category | None, parent_id: int) -> None:
        """Max depth 2: the parent must be top-level, not itself, and the category has no children."""
        depth_error = ValidationFailed(ErrorCode.CATEGORY_DEPTH_EXCEEDED, details={"parent_id": parent_id})
        if category is not None and parent_id == category.id:
            raise depth_error
        parent = await repo.get_category(self.session, parent_id)
        if parent is None:
            raise field_error("parent_id", "category not found", ErrorCode.CATEGORY_NOT_FOUND)
        if parent.parent_id is not None:
            raise depth_error
        if category is not None and await repo.count_children(self.session, category.id):
            raise depth_error

    async def _revalidate_products(self, category_id: int, new_parent_id: int | None) -> None:
        """Moving a category changes inherited definitions: every product must still be valid."""
        ids = [category_id] if new_parent_id is None else [category_id, new_parent_id]
        definitions = merge_definitions(category_id, new_parent_id, await load_definition_rows(self.session, ids))
        await revalidate_products(self.session, [category_id], definitions)

    # ------------------------------------------------------------------ attributes

    async def add_attribute(self, category_id: int, data: AttributeCreate) -> AdminAttributeOut:
        category = await self._get(category_id, for_update=True)
        if await repo.attribute_key_exists(self.session, category.id, data.key):
            raise Conflict(ErrorCode.ATTRIBUTE_KEY_TAKEN, details={"key": data.key})
        _check_attribute_options(data.type, data.options)
        attribute = CategoryAttribute(category_id=category.id, **data.model_dump())
        self.session.add(attribute)
        await flush_catalog(self.session)
        await commit_and_publish(self.session)
        return AdminAttributeOut.model_validate(attribute)

    async def update_attribute(self, category_id: int, attribute_id: int, data: AttributeUpdate) -> AdminAttributeOut:
        category = await self._get(category_id, for_update=True)
        attribute = await self._attribute(category.id, attribute_id)
        changes = {k: v for k, v in dump_set(data).items() if v is not None or k in ("options", "unit")}
        new_type = changes.get("type", attribute.type)
        new_options = changes["options"] if "options" in changes else attribute.options
        if new_type not in SELECT_TYPES and "type" in changes and "options" not in changes:
            new_options = None
        _check_attribute_options(new_type, new_options)
        tree = await self._tree_ids(category.id)

        if new_type != attribute.type:
            used = await usage.products_using_attribute(self.session, tree, attribute.key)
            if used:
                raise Conflict(ErrorCode.ATTRIBUTE_IN_USE, details={"key": attribute.key, "products_count": len(used)})
        elif new_type in SELECT_TYPES:
            for value in [v for v in (attribute.options or []) if v not in (new_options or [])]:
                count = await usage.count_products_using_attribute_value(
                    self.session, tree, attribute.key, value, multiselect=new_type == AttributeType.multiselect
                )
                if count:
                    raise Conflict(ErrorCode.OPTION_IN_USE, details={"value": value, "products_count": count})

        for field, value in changes.items():
            setattr(attribute, field, value)
        attribute.type = new_type
        attribute.options = new_options
        await flush_catalog(self.session)
        await commit_and_publish(self.session)
        return AdminAttributeOut.model_validate(attribute)

    async def delete_attribute(self, category_id: int, attribute_id: int, force: bool) -> None:
        category = await self._get(category_id, for_update=True)
        attribute = await self._attribute(category.id, attribute_id)
        tree = await self._tree_ids(category.id)
        used = await usage.products_using_attribute(self.session, tree, attribute.key)
        if used and not force:
            raise Conflict(ErrorCode.ATTRIBUTE_IN_USE, details={"key": attribute.key, "products_count": len(used)})
        if used:
            await usage.remove_attribute_key(self.session, tree, attribute.key)
        await self.session.delete(attribute)
        await self.session.flush()
        if used:
            await refresh_search_text(self.session, used)
        await commit_and_publish(self.session)

    async def _attribute(self, category_id: int, attribute_id: int) -> CategoryAttribute:
        attribute = await repo.get_attribute(self.session, category_id, attribute_id)
        if attribute is None:
            raise NotFound(ErrorCode.ATTRIBUTE_NOT_FOUND)
        return attribute

    async def _tree_ids(self, category_id: int) -> list[int]:
        return [category_id, *await repo.child_ids(self.session, category_id)]

    # -------------------------------------------------------------- variant options

    async def add_variant_option(self, category_id: int, data: VariantOptionCreate) -> AdminVariantOptionOut:
        category = await self._get(category_id, for_update=True)
        if await repo.variant_option_key_exists(self.session, category.id, data.key):
            raise Conflict(ErrorCode.ATTRIBUTE_KEY_TAKEN, details={"key": data.key})
        option = CategoryVariantOption(category_id=category.id, **data.model_dump())
        self.session.add(option)
        await flush_catalog(self.session)
        await commit_and_publish(self.session)
        return AdminVariantOptionOut.model_validate(option)

    async def update_variant_option(
        self, category_id: int, option_id: int, data: VariantOptionUpdate
    ) -> AdminVariantOptionOut:
        category = await self._get(category_id, for_update=True)
        option = await self._variant_option(category.id, option_id)
        changes = {k: v for k, v in dump_set(data).items() if v is not None or k == "options"}
        new_type = changes.get("type", option.type)
        new_options = changes["options"] if "options" in changes else option.options
        if new_type == VariantOptionType.color and "options" not in changes:
            new_options = None  # switching to color drops the predefined list
        tree = await self._tree_ids(category.id)

        if new_type != option.type:
            count = await usage.count_variants_using_option(self.session, tree, option.key)
            if count:
                raise Conflict(ErrorCode.VARIANT_OPTION_IN_USE, details={"key": option.key, "variants_count": count})
        if new_type == VariantOptionType.color and new_options:
            raise field_error("options", "color options take free color names; options must be null")
        if new_type == option.type == VariantOptionType.select and new_options:
            for value in [v for v in (option.options or []) if v not in new_options]:
                count = await usage.count_variants_using_option_value(self.session, tree, option.key, value)
                if count:
                    raise Conflict(ErrorCode.OPTION_IN_USE, details={"value": value, "products_count": count})
            # Values used by variants must stay in a non-empty predefined list.
            await self._check_used_values_allowed(tree, option.key, new_options)

        for field, value in changes.items():
            setattr(option, field, value)
        option.type = new_type
        option.options = new_options
        await flush_catalog(self.session)
        await commit_and_publish(self.session)
        return AdminVariantOptionOut.model_validate(option)

    async def _check_used_values_allowed(self, tree: list[int], key: str, allowed: Sequence[str]) -> None:
        values = await self.session.scalars(
            select(func.json_unquote(func.json_extract(ProductVariant.options, json_path(key))))
            .join(Product, Product.id == ProductVariant.product_id)
            .where(Product.category_id.in_(tree))
            .distinct()
        )
        for value in values.all():
            if value is not None and value not in allowed:
                count = await usage.count_variants_using_option_value(self.session, tree, key, value)
                raise Conflict(ErrorCode.OPTION_IN_USE, details={"value": value, "products_count": count})

    async def delete_variant_option(self, category_id: int, option_id: int) -> None:
        category = await self._get(category_id, for_update=True)
        option = await self._variant_option(category.id, option_id)
        count = await usage.count_variants_using_option(self.session, await self._tree_ids(category.id), option.key)
        if count:
            raise Conflict(ErrorCode.VARIANT_OPTION_IN_USE, details={"key": option.key, "variants_count": count})
        await self.session.delete(option)
        await commit_and_publish(self.session)

    async def _variant_option(self, category_id: int, option_id: int) -> CategoryVariantOption:
        option = await repo.get_variant_option(self.session, category_id, option_id)
        if option is None:
            raise NotFound(ErrorCode.ATTRIBUTE_NOT_FOUND)
        return option


def _check_attribute_options(kind: AttributeType, options: list[str] | None) -> None:
    if kind in SELECT_TYPES and not options:
        raise field_error("options", "select and multiselect need a non-empty list of options")
    if kind not in SELECT_TYPES and options is not None:
        raise field_error("options", "only select and multiselect attributes have options")


async def revalidate_products(
    session: AsyncSession, category_ids: Sequence[int], definitions: EffectiveDefinitions
) -> None:
    """Check every product (and its variants) in the categories against the definitions."""
    products = (await session.scalars(select(Product).where(Product.category_id.in_(category_ids)))).all()
    if not products:
        return
    variants: dict[int, list[dict[str, Any]]] = {}
    for v in await session.execute(
        select(ProductVariant.product_id, ProductVariant.options).where(
            ProductVariant.product_id.in_([p.id for p in products])
        )
    ):
        variants.setdefault(v.product_id, []).append(v.options)
    for product in products:
        try:
            validate_attributes(definitions, product.attributes)
            cleaned = [validate_variant_options(definitions, o) for o in variants.get(product.id, [])]
            validate_variant_set(cleaned)
        except AppError as exc:
            details = exc.details if isinstance(exc.details, dict) else {"errors": exc.details}
            exc.details = {**details, "product_id": product.id}
            raise
