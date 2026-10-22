import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock, security
from app.modules.admin import repository as admin_repo
from app.modules.admin.schemas import AdminUserOut, AdminUserUpdate, TemporaryPasswordOut
from app.modules.auth import repository as auth_repo
from app.modules.auth.enums import RevokeReason
from app.modules.auth.service import hash_password_async
from app.modules.users.enums import AuthProvider, UserRole
from app.modules.users.models import User
from app.shared.dependencies.pagination import Page, PageParams
from app.shared.exceptions import ErrorCode, NotFound, ValidationFailed

logger = logging.getLogger(__name__)


def to_admin_out(user: User, orders_count: int) -> AdminUserOut:
    return AdminUserOut.model_validate(
        {**_user_fields(user), "orders_count": orders_count}
    )


def _user_fields(user: User) -> dict[str, object]:
    return {field: getattr(user, field) for field in AdminUserOut.model_fields if field != "orders_count"}


class AdminUserService:
    def __init__(self, session: AsyncSession, admin: User) -> None:
        self.session = session
        self.admin = admin

    async def list_users(
        self, params: PageParams, search: str | None, role: UserRole | None, is_active: bool | None
    ) -> Page[AdminUserOut]:
        rows, total = await admin_repo.list_users(
            self.session,
            search=search.strip() if search and search.strip() else None,
            role=role,
            is_active=is_active,
            offset=params.offset,
            limit=params.page_size,
        )
        return Page[AdminUserOut](
            items=[to_admin_out(user, count) for user, count in rows],
            page=params.page,
            page_size=params.page_size,
            total=total,
        )

    async def get_user(self, user_id: int) -> AdminUserOut:
        user = await self._get(user_id)
        return to_admin_out(user, await admin_repo.count_orders(self.session, user.id))

    async def update_user(self, user_id: int, data: AdminUserUpdate) -> AdminUserOut:
        user = await self._get(user_id, for_update=True)
        if user.id == self.admin.id and (
            data.is_active is False or (data.role is not None and data.role != UserRole.admin)
        ):
            raise ValidationFailed(ErrorCode.CANNOT_MODIFY_SELF)

        if data.role is not None:
            user.role = data.role
        if data.is_active is not None and data.is_active != user.is_active:
            user.is_active = data.is_active
            if not data.is_active:
                await auth_repo.revoke_user_tokens(
                    self.session, user.id, RevokeReason.deactivated, clock.utcnow()
                )
        await self.session.commit()
        logger.info(
            "Admin %s updated user %s: is_active=%s role=%s", self.admin.id, user.id, data.is_active, data.role
        )
        return to_admin_out(user, await admin_repo.count_orders(self.session, user.id))

    async def reset_password(self, user_id: int) -> TemporaryPasswordOut:
        user = await self._get(user_id, for_update=True)
        if user.auth_provider != AuthProvider.phone:
            raise ValidationFailed(ErrorCode.NOT_A_PASSWORD_ACCOUNT)
        temporary = security.generate_temporary_password()
        user.password_hash = await hash_password_async(temporary)
        user.must_change_password = True
        await auth_repo.revoke_user_tokens(self.session, user.id, RevokeReason.admin_reset, clock.utcnow())
        await self.session.commit()
        logger.info("Admin %s reset the password of user %s", self.admin.id, user.id)
        return TemporaryPasswordOut(temporary_password=temporary)

    async def _get(self, user_id: int, *, for_update: bool = False) -> User:
        user = await admin_repo.get_user(self.session, user_id, for_update=for_update)
        if user is None:
            raise NotFound(ErrorCode.USER_NOT_FOUND)
        return user
