from typing import Annotated

from fastapi import APIRouter, Depends

from app.modules.delivery.schemas import (
    AdminDeliveryMethodOut,
    AdminDeliveryMethodUpdate,
    GovernorateOut,
)
from app.modules.delivery.service import AdminDeliveryService
from app.shared.dependencies.auth import AdminUser, DbSession
from app.shared.governorates import GOVERNORATES

router = APIRouter(tags=["delivery"])
admin_router = APIRouter(prefix="/admin/delivery-methods", tags=["admin-delivery"])


@router.get("/governorates", response_model=list[GovernorateOut])
async def list_governorates() -> list[GovernorateOut]:
    return [GovernorateOut(code=item.code, name=item.name) for item in GOVERNORATES]


def get_admin_service(session: DbSession, admin: AdminUser) -> AdminDeliveryService:
    return AdminDeliveryService(session)


AdminService = Annotated[AdminDeliveryService, Depends(get_admin_service)]


@admin_router.get("", response_model=list[AdminDeliveryMethodOut])
async def list_delivery_methods(service: AdminService) -> list[AdminDeliveryMethodOut]:
    return [AdminDeliveryMethodOut.model_validate(method) for method in await service.list()]


@admin_router.patch("/{method_id}", response_model=AdminDeliveryMethodOut)
async def update_delivery_method(
    method_id: int, data: AdminDeliveryMethodUpdate, service: AdminService
) -> AdminDeliveryMethodOut:
    return AdminDeliveryMethodOut.model_validate(await service.update(method_id, data))
