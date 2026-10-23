from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status

from app.modules.products.enums import ProductType
from app.modules.products.schemas import (
    AdminImageOut,
    AdminProductListItem,
    AdminProductOut,
    AdminProductSort,
    BundleItemsIn,
    ImageDeleteOut,
    ImageOrder,
    ImagesUploadOut,
    ImageUpdate,
    LowStockItem,
    ProductCreate,
    ProductUpdate,
    StockAdjust,
    StockOut,
    VariantIn,
    VariantUpdate,
)
from app.modules.products.service import MAX_FILES_PER_UPLOAD, AdminProductService
from app.shared.catalog_cache import catalog_write
from app.shared.dependencies.auth import AdminUser, DbSession
from app.shared.dependencies.pagination import Page, PageParamsDep
from app.shared.utils.uploads import limit_upload_body, read_upload

router = APIRouter(prefix="/admin/products", tags=["admin-catalog"], dependencies=[Depends(catalog_write)])


def get_service(session: DbSession, admin: AdminUser) -> AdminProductService:
    return AdminProductService(session)


Service = Annotated[AdminProductService, Depends(get_service)]


@router.get("", response_model=Page[AdminProductListItem])
async def list_products(
    service: Service,
    params: PageParamsDep,
    q: Annotated[str | None, Query(max_length=200, description="Search text or exact SKU")] = None,
    category_id: Annotated[int | None, Query(description="Includes child categories")] = None,
    brand_id: int | None = None,
    type: ProductType | None = None,
    is_active: bool | None = None,
    in_stock: bool | None = None,
    low_stock: bool = False,
    sort: AdminProductSort = AdminProductSort.newest,
) -> Page[AdminProductListItem]:
    return await service.list(
        params, q=q, category_id=category_id, brand_id=brand_id, product_type=type, is_active=is_active,
        in_stock=in_stock, low_stock=low_stock, sort=sort,
    )


@router.get("/low-stock", response_model=Page[LowStockItem])
async def low_stock(
    service: Service,
    params: PageParamsDep,
    threshold: Annotated[int | None, Query(ge=0, le=1_000_000, description="Default: store setting")] = None,
) -> Page[LowStockItem]:
    """Active simple products and active variants at or below the threshold."""
    return await service.low_stock(params, threshold)


@router.post("", response_model=AdminProductOut, status_code=status.HTTP_201_CREATED)
async def create_product(data: ProductCreate, service: Service) -> AdminProductOut:
    """Creates the product (and variants / bundle items) in one transaction. Always inactive:
    add images, then PATCH is_active=true to publish."""
    return await service.create(data)


@router.get("/{product_id}", response_model=AdminProductOut)
async def get_product(product_id: int, service: Service) -> AdminProductOut:
    return await service.get(product_id)


@router.patch("/{product_id}", response_model=AdminProductOut)
async def update_product(product_id: int, data: ProductUpdate, service: Service) -> AdminProductOut:
    """`attributes`, when sent, replaces the whole object. is_active=true runs the publish check."""
    return await service.update(product_id, data)


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(product_id: int, service: Service) -> Response:
    await service.delete(product_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{product_id}/stock-adjust", response_model=StockOut)
async def adjust_stock(product_id: int, data: StockAdjust, service: Service) -> StockOut:
    return await service.adjust_stock(product_id, data.delta)


@router.post("/{product_id}/variants", response_model=AdminProductOut, status_code=status.HTTP_201_CREATED)
async def add_variant(product_id: int, data: VariantIn, service: Service) -> AdminProductOut:
    return await service.add_variant(product_id, data)


@router.patch("/{product_id}/variants/{variant_id}", response_model=AdminProductOut)
async def update_variant(product_id: int, variant_id: int, data: VariantUpdate, service: Service) -> AdminProductOut:
    return await service.update_variant(product_id, variant_id, data)


@router.delete("/{product_id}/variants/{variant_id}", response_model=AdminProductOut)
async def delete_variant(product_id: int, variant_id: int, service: Service) -> AdminProductOut:
    return await service.delete_variant(product_id, variant_id)


@router.post("/{product_id}/variants/{variant_id}/stock-adjust", response_model=StockOut)
async def adjust_variant_stock(product_id: int, variant_id: int, data: StockAdjust, service: Service) -> StockOut:
    return await service.adjust_variant_stock(product_id, variant_id, data.delta)


@router.put("/{product_id}/bundle-items", response_model=AdminProductOut)
async def replace_bundle_items(product_id: int, data: BundleItemsIn, service: Service) -> AdminProductOut:
    """Replaces the bundle's contents as a whole."""
    return await service.replace_bundle_items(product_id, data.items)


@router.post(
    "/{product_id}/images",
    response_model=ImagesUploadOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_upload_body(MAX_FILES_PER_UPLOAD))],
)
async def upload_images(
    product_id: int,
    service: Service,
    files: Annotated[list[UploadFile], File(description="1..8 images (JPEG, PNG, WEBP)")],
    variant_id: Annotated[int | None, Form()] = None,
    color: Annotated[str | None, Form(max_length=50, description="Link to the first active variant of this color")] = None,
) -> ImagesUploadOut:
    data = [await read_upload(f) for f in files[: MAX_FILES_PER_UPLOAD + 1]]
    return await service.upload_images(product_id, data, variant_id, color.strip() if color else None)


@router.patch("/{product_id}/images/{image_id}", response_model=AdminImageOut)
async def update_image(product_id: int, image_id: int, data: ImageUpdate, service: Service) -> AdminImageOut:
    return await service.update_image(product_id, image_id, data)


@router.put("/{product_id}/images/order", response_model=list[AdminImageOut])
async def reorder_images(product_id: int, data: ImageOrder, service: Service) -> list[AdminImageOut]:
    """image_ids must contain exactly the product's images; the first becomes the main image."""
    return await service.reorder_images(product_id, data.image_ids)


@router.delete("/{product_id}/images/{image_id}", response_model=ImageDeleteOut)
async def delete_image(product_id: int, image_id: int, service: Service) -> ImageDeleteOut:
    return await service.delete_image(product_id, image_id)
