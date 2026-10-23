from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile, status
from pydantic import TypeAdapter

from app.modules.banners.schemas import AdminBannerOut, BannerOrder, BannerOut, BannerUpdate
from app.modules.banners.service import AdminBannerService, BannerService
from app.modules.notifications.schemas import LinkIn, to_naive_utc
from app.shared.catalog_cache import catalog_write
from app.shared.http_cache import serve_cached
from app.shared.dependencies.auth import AdminUser, DbSession
from app.shared.enums import LinkType
from app.shared.utils.uploads import limit_upload_body, read_upload

router = APIRouter(prefix="/banners", tags=["banners"])
admin_router = APIRouter(prefix="/admin/banners", tags=["admin-banners"], dependencies=[Depends(catalog_write)])


def get_service(session: DbSession) -> BannerService:
    return BannerService(session)


def get_admin_service(session: DbSession, admin: AdminUser) -> AdminBannerService:
    return AdminBannerService(session)


Service = Annotated[BannerService, Depends(get_service)]
Admin = Annotated[AdminBannerService, Depends(get_admin_service)]


BANNERS_ADAPTER: TypeAdapter[list[BannerOut]] = TypeAdapter(list[BannerOut])


@router.get("", response_model=list[BannerOut])
async def list_banners(request: Request, service: Service) -> Response:
    """Active banners in their time window (at most 10), in the admin's order. Banners linking
    to a hidden category / product / brand are skipped."""
    return await serve_cached(request, "banners", [], BANNERS_ADAPTER, service.public)


@admin_router.get("", response_model=list[AdminBannerOut])
async def admin_list_banners(service: Admin) -> list[AdminBannerOut]:
    return await service.list()


@admin_router.post("", response_model=AdminBannerOut, status_code=status.HTTP_201_CREATED,
                   dependencies=[Depends(limit_upload_body(1))])
async def create_banner(
    service: Admin,
    image: Annotated[UploadFile, File(description="JPEG, PNG or WEBP")],
    title: Annotated[str, Form(min_length=1, max_length=150)],
    subtitle: Annotated[str | None, Form(max_length=255)] = None,
    link_type: Annotated[LinkType, Form()] = LinkType.none,
    link_value: Annotated[str | None, Form(max_length=255)] = None,
    # A string on purpose: FastAPI reads a MISSING form boolean as false (checkbox semantics).
    is_active: Annotated[str | None, Form(pattern="^(true|false|1|0)$", description="true (default) or false")] = None,
    starts_at: Annotated[datetime | None, Form(description="UTC")] = None,
    ends_at: Annotated[datetime | None, Form(description="UTC")] = None,
) -> AdminBannerOut:
    """Multipart. The new banner goes last; use PUT /admin/banners/order to move it."""
    return await service.create(
        title=title, subtitle=subtitle, link=LinkIn(type=link_type, value=link_value),
        is_active=is_active is None or is_active in ("true", "1"),
        starts_at=to_naive_utc(starts_at), ends_at=to_naive_utc(ends_at), image=await read_upload(image),
    )


@admin_router.put("/order", response_model=list[AdminBannerOut])
async def reorder_banners(data: BannerOrder, service: Admin) -> list[AdminBannerOut]:
    """banner_ids must contain every banner exactly once (422 BANNER_ORDER_MISMATCH)."""
    return await service.reorder(data.banner_ids)


@admin_router.get("/{banner_id}", response_model=AdminBannerOut)
async def admin_get_banner(banner_id: int, service: Admin) -> AdminBannerOut:
    return await service.get(banner_id)


@admin_router.patch("/{banner_id}", response_model=AdminBannerOut)
async def update_banner(banner_id: int, data: BannerUpdate, service: Admin) -> AdminBannerOut:
    return await service.update(banner_id, data)


@admin_router.put("/{banner_id}/image", response_model=AdminBannerOut, dependencies=[Depends(limit_upload_body(1))])
async def replace_banner_image(banner_id: int, image: Annotated[UploadFile, File()], service: Admin) -> AdminBannerOut:
    return await service.set_image(banner_id, await read_upload(image))


@admin_router.delete("/{banner_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_banner(banner_id: int, service: Admin) -> Response:
    await service.delete(banner_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
