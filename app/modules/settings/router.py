from typing import Annotated

from fastapi import APIRouter, Depends

from app.modules.settings.admin import AdminSettingsService
from app.modules.settings.schemas import AdminSettingsOut, AdminSettingsUpdate
from app.shared.catalog_cache import catalog_write
from app.shared.dependencies.auth import AdminUser, DbSession

admin_router = APIRouter(prefix="/admin/settings", tags=["admin"], dependencies=[Depends(catalog_write)])


def get_service(session: DbSession, admin: AdminUser) -> AdminSettingsService:
    return AdminSettingsService(session)


Service = Annotated[AdminSettingsService, Depends(get_service)]


@admin_router.get("", response_model=AdminSettingsOut)
async def get_settings(service: Service) -> AdminSettingsOut:
    """The store settings managed here, with their current values (defaults when not stored)."""
    return await service.get()


@admin_router.patch("", response_model=AdminSettingsOut)
async def update_settings(data: AdminSettingsUpdate, service: Service) -> AdminSettingsOut:
    """Change one or more settings. Every change invalidates the catalog caches."""
    return await service.update(data)
