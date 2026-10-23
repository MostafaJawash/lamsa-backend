from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status

from app.modules.brands.schemas import AdminBrandOut, BrandCreate, BrandOut, BrandUpdate
from app.modules.brands.service import AdminBrandService, BrandCatalogService
from app.shared.dependencies.auth import AdminUser, DbSession
from app.shared.dependencies.pagination import Page, PageParamsDep
from app.shared.utils.uploads import limit_upload_body, read_upload

router = APIRouter(prefix="/brands", tags=["catalog"])
admin_router = APIRouter(prefix="/admin/brands", tags=["admin-catalog"])


def get_catalog_service(session: DbSession) -> BrandCatalogService:
    return BrandCatalogService(session)


def get_admin_service(session: DbSession, admin: AdminUser) -> AdminBrandService:
    return AdminBrandService(session)


Catalog = Annotated[BrandCatalogService, Depends(get_catalog_service)]
Admin = Annotated[AdminBrandService, Depends(get_admin_service)]


@router.get("", response_model=list[BrandOut])
async def list_brands(
    service: Catalog,
    category: Annotated[str | None, Query(description="Category slug: only brands with products there")] = None,
) -> list[BrandOut]:
    return await service.list(category)


@router.get("/{slug}", response_model=BrandOut)
async def get_brand(slug: str, service: Catalog) -> BrandOut:
    return await service.detail(slug)


@admin_router.get("", response_model=Page[AdminBrandOut])
async def admin_list_brands(
    service: Admin,
    params: PageParamsDep,
    search: Annotated[str | None, Query(max_length=100)] = None,
    is_active: bool | None = None,
) -> Page[AdminBrandOut]:
    return await service.list(params, search, is_active)


@admin_router.post("", response_model=AdminBrandOut, status_code=status.HTTP_201_CREATED)
async def create_brand(data: BrandCreate, service: Admin) -> AdminBrandOut:
    return await service.create(data)


@admin_router.get("/{brand_id}", response_model=AdminBrandOut)
async def admin_get_brand(brand_id: int, service: Admin) -> AdminBrandOut:
    return await service.get(brand_id)


@admin_router.patch("/{brand_id}", response_model=AdminBrandOut)
async def update_brand(brand_id: int, data: BrandUpdate, service: Admin) -> AdminBrandOut:
    return await service.update(brand_id, data)


@admin_router.delete("/{brand_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_brand(brand_id: int, service: Admin) -> Response:
    await service.delete(brand_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@admin_router.put("/{brand_id}/logo", response_model=AdminBrandOut, dependencies=[Depends(limit_upload_body(1))])
async def set_brand_logo(brand_id: int, file: Annotated[UploadFile, File()], service: Admin) -> AdminBrandOut:
    return await service.set_logo(brand_id, await read_upload(file))


@admin_router.delete("/{brand_id}/logo", response_model=AdminBrandOut)
async def delete_brand_logo(brand_id: int, service: Admin) -> AdminBrandOut:
    return await service.delete_logo(brand_id)
