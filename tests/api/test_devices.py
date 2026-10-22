import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.users.models import UserDevice
from conftest import FakeClock, bearer, register

pytestmark = pytest.mark.anyio

BASE = "/api/v1/users/me/devices"


async def device(session: AsyncSession, token: str) -> UserDevice | None:
    return await session.scalar(
        select(UserDevice).where(UserDevice.fcm_token == token).execution_options(populate_existing=True)
    )


async def test_register_device_upserts(client: AsyncClient, db_session: AsyncSession, frozen_clock: FakeClock) -> None:
    user = await register(client, 89)
    headers = bearer(user["access_token"])

    assert (await client.post(BASE, json={"fcm_token": "tok-a", "platform": "android"}, headers=headers)).status_code == 204
    first = await device(db_session, "tok-a")
    assert first is not None and first.user_id == user["user"]["id"]

    frozen_clock.advance(hours=1)
    assert (await client.post(BASE, json={"fcm_token": "tok-a", "platform": "web"}, headers=headers)).status_code == 204
    again = await device(db_session, "tok-a")
    assert again is not None and again.id == first.id
    assert again.platform == "web" and again.last_seen_at == frozen_clock.now


async def test_token_moves_to_new_user(client: AsyncClient, db_session: AsyncSession) -> None:
    alice = await register(client, 90)
    bob = await register(client, 91)
    await client.post(BASE, json={"fcm_token": "shared", "platform": "ios"}, headers=bearer(alice["access_token"]))
    await client.post(BASE, json={"fcm_token": "shared", "platform": "ios"}, headers=bearer(bob["access_token"]))

    moved = await device(db_session, "shared")
    assert moved is not None and moved.user_id == bob["user"]["id"]
    count = (await db_session.scalars(select(UserDevice).where(UserDevice.fcm_token == "shared"))).all()
    assert len(count) == 1


async def test_unregister(client: AsyncClient, db_session: AsyncSession) -> None:
    alice = await register(client, 92)
    bob = await register(client, 93)
    await client.post(BASE, json={"fcm_token": "a-tok", "platform": "android"}, headers=bearer(alice["access_token"]))

    # Someone else's token is not removed, but the answer is still 204.
    response = await client.post(f"{BASE}/unregister", json={"fcm_token": "a-tok"}, headers=bearer(bob["access_token"]))
    assert response.status_code == 204
    assert await device(db_session, "a-tok") is not None

    response = await client.post(f"{BASE}/unregister", json={"fcm_token": "a-tok"}, headers=bearer(alice["access_token"]))
    assert response.status_code == 204
    assert await device(db_session, "a-tok") is None

    response = await client.post(f"{BASE}/unregister", json={"fcm_token": "never"}, headers=bearer(alice["access_token"]))
    assert response.status_code == 204


async def test_invalid_platform(client: AsyncClient) -> None:
    user = await register(client, 94)
    response = await client.post(BASE, json={"fcm_token": "x", "platform": "symbian"}, headers=bearer(user["access_token"]))
    assert response.status_code == 422
