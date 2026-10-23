"""Helpers for notification and banner tests."""

from typing import Any

from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.models import Notification
from app.modules.users.enums import UserRole
from app.modules.users.models import User

NOTIFICATIONS = "/api/v1/notifications"
PREFERENCES = "/api/v1/users/me/notification-preferences"
ADMIN_NOTIFICATIONS = "/api/v1/admin/notifications"
BANNERS = "/api/v1/banners"
ADMIN_BANNERS = "/api/v1/admin/banners"


async def me(client: AsyncClient, headers: dict[str, str]) -> int:
    response = await client.get("/api/v1/users/me", headers=headers)
    assert response.status_code == 200, response.text
    user_id: int = response.json()["id"]
    return user_id


async def add_device(client: AsyncClient, headers: dict[str, str], token: str, platform: str = "android") -> None:
    response = await client.post("/api/v1/users/me/devices", json={"fcm_token": token, "platform": platform}, headers=headers)
    assert response.status_code == 204, response.text


async def rows(session: AsyncSession, *where: Any) -> list[Notification]:
    """Notifications as stored now (fresh from the database), oldest first."""
    result = await session.scalars(
        select(Notification).where(*where).order_by(Notification.id).execution_options(populate_existing=True)
    )
    return list(result)


async def set_user(session: AsyncSession, user_id: int, **values: Any) -> None:
    await session.execute(update(User).where(User.id == user_id).values(**values))
    await session.commit()


async def demote(session: AsyncSession, user_id: int) -> None:
    await set_user(session, user_id, role=UserRole.customer)


__all__ = ["ADMIN_BANNERS", "ADMIN_NOTIFICATIONS", "BANNERS", "NOTIFICATIONS", "PREFERENCES", "add_device", "demote",
           "me", "rows", "set_user"]
