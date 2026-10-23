from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.storage.transaction import MediaTransaction
from app.modules.brands import repository as repo
from app.modules.brands.models import Brand
from app.modules.brands.schemas import AdminBrandOut, BrandCreate, BrandOut, BrandUpdate
from app.modules.categories.tree import load_category_index
from app.modules.products.summary import refresh_search_text
from app.shared.catalog import flush_catalog, unique_slug
from app.shared.dependencies.pagination import Page, PageParams
from app.shared.exceptions import Conflict, ErrorCode, NotFound
from app.shared.schemas import image_urls


def brand_out(brand: Brand, count: int) -> BrandOut:
    return BrandOut(
        id=brand.id, name=brand.name, slug=brand.slug, description=brand.description,
        logo=image_urls(brand.logo_key), is_own_brand=brand.is_own_brand, product_count=count,
    )


def admin_brand_out(brand: Brand, count: int) -> AdminBrandOut:
    return AdminBrandOut(
        **brand_out(brand, count).model_dump(), is_active=brand.is_active, sort_order=brand.sort_order,
        created_at=brand.created_at, updated_at=brand.updated_at,
    )


class BrandCatalogService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self, category_slug: str | None) -> list[BrandOut]:
        category_ids: list[int] | None = None
        if category_slug is not None:
            index = await load_category_index(self.session)
            category = index.by_slug(category_slug)
            if category is None or not index.is_visible(category):
                raise NotFound(ErrorCode.CATEGORY_NOT_FOUND)
            category_ids = index.subtree_ids(category)
        rows = await repo.visible_brands_with_counts(self.session, category_ids)
        return [brand_out(brand, count) for brand, count in rows]

    async def detail(self, slug: str) -> BrandOut:
        row = await repo.visible_brand_by_slug(self.session, slug)
        if row is None:
            raise NotFound(ErrorCode.BRAND_NOT_FOUND)
        return brand_out(*row)


class AdminBrandService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self, params: PageParams, search: str | None, is_active: bool | None) -> Page[AdminBrandOut]:
        rows, total = await repo.admin_list(
            self.session, search=(search or "").strip() or None, is_active=is_active,
            offset=params.offset, limit=params.page_size,
        )
        return Page[AdminBrandOut](
            items=[admin_brand_out(b, c) for b, c in rows], page=params.page, page_size=params.page_size, total=total
        )

    async def get(self, brand_id: int) -> AdminBrandOut:
        brand = await self._get(brand_id)
        return admin_brand_out(brand, await repo.count_products(self.session, brand.id))

    async def create(self, data: BrandCreate) -> AdminBrandOut:
        slug = await unique_slug(self.session, Brand, slug=data.slug, name=data.name, fallback_prefix="brand")
        brand = Brand(**data.model_dump(exclude={"slug"}), slug=slug)
        self.session.add(brand)
        await flush_catalog(self.session)
        await self.session.commit()
        return admin_brand_out(brand, 0)

    async def update(self, brand_id: int, data: BrandUpdate) -> AdminBrandOut:
        brand = await self._get(brand_id, for_update=True)
        changes = data.model_dump(exclude_unset=True)
        renamed = "name" in changes and changes["name"] is not None and changes["name"] != brand.name
        if "slug" in changes and changes["slug"] is not None and changes["slug"] != brand.slug:
            brand.slug = await unique_slug(
                self.session, Brand, slug=changes["slug"], name=brand.name, fallback_prefix="brand", exclude_id=brand.id
            )
        for field, value in changes.items():
            if field != "slug" and (value is not None or field == "description"):
                setattr(brand, field, value)
        await flush_catalog(self.session)
        if renamed:
            await refresh_search_text(self.session, await repo.product_ids(self.session, brand.id))
        await self.session.commit()
        return await self.get(brand.id)

    async def delete(self, brand_id: int) -> None:
        brand = await self._get(brand_id, for_update=True)
        count = await repo.count_products(self.session, brand.id)
        if count:
            raise Conflict(ErrorCode.BRAND_HAS_PRODUCTS, details={"products": count})
        media = MediaTransaction(self.session)
        media.delete_after_commit(brand.logo_key)
        await self.session.delete(brand)
        await media.commit()

    async def set_logo(self, brand_id: int, data: bytes) -> AdminBrandOut:
        brand = await self._get(brand_id, for_update=True)
        media = MediaTransaction(self.session)
        saved = await media.save_image(f"brands/{brand.id}", data)
        media.delete_after_commit(brand.logo_key)
        brand.logo_key = saved.image_key
        await media.commit()
        return await self.get(brand.id)

    async def delete_logo(self, brand_id: int) -> AdminBrandOut:
        brand = await self._get(brand_id, for_update=True)
        media = MediaTransaction(self.session)
        media.delete_after_commit(brand.logo_key)
        brand.logo_key = None
        await media.commit()
        return await self.get(brand.id)

    async def _get(self, brand_id: int, *, for_update: bool = False) -> Brand:
        brand = await repo.get_brand(self.session, brand_id, for_update=for_update)
        if brand is None:
            raise NotFound(ErrorCode.BRAND_NOT_FOUND)
        return brand
