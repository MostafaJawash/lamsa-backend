from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status
from pydantic import TypeAdapter

from app.modules.categories.schemas import (
    AdminAttributeOut,
    AdminCategoryDetail,
    AdminCategoryNode,
    AdminVariantOptionOut,
    AttributeCreate,
    AttributeUpdate,
    CategoryCreate,
    CategoryDetailOut,
    CategoryNode,
    CategoryUpdate,
    VariantOptionCreate,
    VariantOptionUpdate,
)
from app.modules.categories.service import AdminCategoryService, CategoryCatalogService
from app.shared.catalog_cache import catalog_write
from app.shared.http_cache import serve_cached
from app.shared.dependencies.auth import AdminUser, DbSession
from app.shared.utils.uploads import limit_upload_body, read_upload

router = APIRouter(prefix="/categories", tags=["catalog"])
admin_router = APIRouter(prefix="/admin/categories", tags=["admin-catalog"], dependencies=[Depends(catalog_write)])


def get_catalog_service(session: DbSession) -> CategoryCatalogService:
    return CategoryCatalogService(session)


def get_admin_service(session: DbSession, admin: AdminUser) -> AdminCategoryService:
    return AdminCategoryService(session)


Catalog = Annotated[CategoryCatalogService, Depends(get_catalog_service)]
Admin = Annotated[AdminCategoryService, Depends(get_admin_service)]
SingleUpload = Depends(limit_upload_body(1))

# ------------------------------------------------------------------------------- public


TREE_ADAPTER: TypeAdapter[list[CategoryNode]] = TypeAdapter(list[CategoryNode])
DETAIL_ADAPTER: TypeAdapter[CategoryDetailOut] = TypeAdapter(CategoryDetailOut)


@router.get("", response_model=list[CategoryNode])
async def list_categories(request: Request, service: Catalog) -> Response:
    """Visible categories as a tree, with visible product counts (own + children)."""
    return await serve_cached(request, "categories", [], TREE_ADAPTER, service.tree)


@router.get("/{slug}", response_model=CategoryDetailOut)
async def get_category(request: Request, slug: str, service: Catalog) -> Response:
    """Category with breadcrumb, visible children and its effective filter definitions."""
    return await serve_cached(request, "category", [slug], DETAIL_ADAPTER, lambda: service.detail(slug))


# -------------------------------------------------------------------------------- admin


@admin_router.get("", response_model=list[AdminCategoryNode])
async def admin_tree(service: Admin) -> list[AdminCategoryNode]:
    return await service.tree()


@admin_router.post("", response_model=AdminCategoryDetail, status_code=status.HTTP_201_CREATED)
async def create_category(data: CategoryCreate, service: Admin) -> AdminCategoryDetail:
    return await service.create(data)


@admin_router.get("/{category_id}", response_model=AdminCategoryDetail)
async def admin_get_category(category_id: int, service: Admin) -> AdminCategoryDetail:
    return await service.detail(category_id)


@admin_router.patch("/{category_id}", response_model=AdminCategoryDetail)
async def update_category(category_id: int, data: CategoryUpdate, service: Admin) -> AdminCategoryDetail:
    return await service.update(category_id, data)


@admin_router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(category_id: int, service: Admin) -> Response:
    """Only empty categories; deactivate instead when it has children or products."""
    await service.delete(category_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@admin_router.put("/{category_id}/image", response_model=AdminCategoryDetail, dependencies=[SingleUpload])
async def set_category_image(
    category_id: int, file: Annotated[UploadFile, File()], service: Admin
) -> AdminCategoryDetail:
    return await service.set_image(category_id, await read_upload(file))


@admin_router.delete("/{category_id}/image", response_model=AdminCategoryDetail)
async def delete_category_image(category_id: int, service: Admin) -> AdminCategoryDetail:
    return await service.delete_image(category_id)


@admin_router.post(
    "/{category_id}/attributes", response_model=AdminAttributeOut, status_code=status.HTTP_201_CREATED
)
async def add_attribute(category_id: int, data: AttributeCreate, service: Admin) -> AdminAttributeOut:
    return await service.add_attribute(category_id, data)


@admin_router.patch("/{category_id}/attributes/{attribute_id}", response_model=AdminAttributeOut)
async def update_attribute(
    category_id: int, attribute_id: int, data: AttributeUpdate, service: Admin
) -> AdminAttributeOut:
    return await service.update_attribute(category_id, attribute_id, data)


@admin_router.delete("/{category_id}/attributes/{attribute_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attribute(
    category_id: int,
    attribute_id: int,
    service: Admin,
    force: Annotated[bool, Query(description="Also remove the key from every product using it")] = False,
) -> Response:
    await service.delete_attribute(category_id, attribute_id, force)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@admin_router.post(
    "/{category_id}/variant-options", response_model=AdminVariantOptionOut, status_code=status.HTTP_201_CREATED
)
async def add_variant_option(category_id: int, data: VariantOptionCreate, service: Admin) -> AdminVariantOptionOut:
    return await service.add_variant_option(category_id, data)


@admin_router.patch("/{category_id}/variant-options/{option_id}", response_model=AdminVariantOptionOut)
async def update_variant_option(
    category_id: int, option_id: int, data: VariantOptionUpdate, service: Admin
) -> AdminVariantOptionOut:
    return await service.update_variant_option(category_id, option_id, data)


@admin_router.delete("/{category_id}/variant-options/{option_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_variant_option(category_id: int, option_id: int, service: Admin) -> Response:
    await service.delete_variant_option(category_id, option_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
