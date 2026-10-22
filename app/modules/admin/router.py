from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.modules.admin.schemas import AdminUserOut, AdminUserUpdate, TemporaryPasswordOut
from app.modules.admin.service import AdminUserService
from app.modules.users.enums import UserRole
from app.shared.dependencies.auth import AdminUser, DbSession
from app.shared.dependencies.pagination import Page, PageParamsDep

router = APIRouter(prefix="/admin", tags=["admin"])


def get_admin_user_service(session: DbSession, admin: AdminUser) -> AdminUserService:
    return AdminUserService(session, admin)


Service = Annotated[AdminUserService, Depends(get_admin_user_service)]


@router.get("/users", response_model=Page[AdminUserOut])
async def list_users(
    service: Service,
    params: PageParamsDep,
    search: Annotated[str | None, Query(max_length=100, description="Part of name or phone")] = None,
    role: UserRole | None = None,
    is_active: bool | None = None,
) -> Page[AdminUserOut]:
    """Users, newest first."""
    return await service.list_users(params, search, role, is_active)


@router.get("/users/{user_id}", response_model=AdminUserOut)
async def get_user(user_id: int, service: Service) -> AdminUserOut:
    return await service.get_user(user_id)


@router.patch("/users/{user_id}", response_model=AdminUserOut)
async def update_user(user_id: int, data: AdminUserUpdate, service: Service) -> AdminUserOut:
    """Activate/deactivate or change role. Deactivating ends all of the user's sessions."""
    return await service.update_user(user_id, data)


@router.post("/users/{user_id}/reset-password", response_model=TemporaryPasswordOut)
async def reset_password(user_id: int, service: Service) -> TemporaryPasswordOut:
    """Set a temporary password (shown once) for a phone account that forgot its password.
    The user must change it at next login; all their sessions are ended."""
    return await service.reset_password(user_id)
