from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.config import settings
from app.modules.auth.enums import RevokeReason
from app.modules.auth.models import RefreshToken
from conftest import FakeClock, bearer, error_code, login, register

pytestmark = pytest.mark.anyio

GRACE = settings.REFRESH_REUSE_GRACE_SECONDS


async def refresh(client: AsyncClient, token: str) -> object:
    return await client.post("/api/v1/auth/refresh", json={"refresh_token": token})


async def tokens_of(session: AsyncSession, user_id: int) -> list[RefreshToken]:
    result = await session.scalars(
        select(RefreshToken)
        .where(RefreshToken.user_id == user_id)
        .order_by(RefreshToken.id)
        .execution_options(populate_existing=True)
    )
    return list(result.all())


async def test_refresh_rotates_token(client: AsyncClient, db_session: AsyncSession, frozen_clock: FakeClock) -> None:
    data = await register(client, 50)
    frozen_clock.advance(minutes=5)

    response = await refresh(client, data["refresh_token"])
    assert response.status_code == 200, response.text
    new = response.json()
    assert new["refresh_token"] != data["refresh_token"]
    assert (await client.get("/api/v1/users/me", headers=bearer(new["access_token"]))).status_code == 200

    old, current = await tokens_of(db_session, data["user"]["id"])
    assert old.revoked_reason == RevokeReason.rotated
    assert old.replaced_by_id == current.id
    assert old.last_used_at == frozen_clock.now
    assert current.family_id == old.family_id
    assert current.revoked_at is None
    assert old.token_hash == security.hash_token(data["refresh_token"])  # only the hash is stored

    # The new token keeps working.
    assert (await refresh(client, new["refresh_token"])).status_code == 200


async def test_reuse_after_grace_revokes_family(client: AsyncClient, db_session: AsyncSession, frozen_clock: FakeClock) -> None:
    data = await register(client, 51)
    new = (await refresh(client, data["refresh_token"])).json()

    frozen_clock.advance(seconds=GRACE + 1)
    reused = await refresh(client, data["refresh_token"])
    assert reused.status_code == 401
    assert error_code(reused) == "TOKEN_REUSED"

    # The legitimate newest token is now dead too.
    after = await refresh(client, new["refresh_token"])
    assert after.status_code == 401
    assert error_code(after) == "TOKEN_REUSED"
    assert all(t.revoked_at is not None for t in await tokens_of(db_session, data["user"]["id"]))


async def test_duplicate_refresh_within_grace_issues_sibling(
    client: AsyncClient, db_session: AsyncSession, frozen_clock: FakeClock
) -> None:
    data = await register(client, 52)
    first = await refresh(client, data["refresh_token"])
    frozen_clock.advance(seconds=GRACE - 5)
    second = await refresh(client, data["refresh_token"])  # parallel request on app start

    assert first.status_code == 200 and second.status_code == 200, second.text
    a, b = first.json()["refresh_token"], second.json()["refresh_token"]
    assert a != b

    original, *_ = await tokens_of(db_session, data["user"]["id"])
    assert original.revoked_reason == RevokeReason.rotated  # unchanged by the duplicate
    # Both resulting tokens work.
    assert (await refresh(client, a)).status_code == 200
    assert (await refresh(client, b)).status_code == 200


async def test_reuse_of_logged_out_token_within_grace_is_strict(
    client: AsyncClient, frozen_clock: FakeClock
) -> None:
    data = await register(client, 53)
    new = (await refresh(client, data["refresh_token"])).json()
    assert (await client.post("/api/v1/auth/logout", json={"refresh_token": new["refresh_token"]})).status_code == 204

    frozen_clock.advance(seconds=1)
    response = await refresh(client, new["refresh_token"])  # revoked by logout, not rotation
    assert response.status_code == 401
    assert error_code(response) == "TOKEN_REUSED"
    # The rotated original is inside its grace window, but its family is revoked: strict too.
    response = await refresh(client, data["refresh_token"])
    assert error_code(response) == "TOKEN_REUSED"


async def test_unknown_and_expired_refresh_tokens(client: AsyncClient, frozen_clock: FakeClock) -> None:
    unknown = await refresh(client, "definitely-not-a-token")
    assert unknown.status_code == 401
    assert error_code(unknown) == "TOKEN_INVALID"

    data = await register(client, 54)
    frozen_clock.advance(days=settings.REFRESH_TOKEN_DAYS, seconds=1)
    expired = await refresh(client, data["refresh_token"])
    assert expired.status_code == 401
    assert error_code(expired) == "TOKEN_INVALID"


async def test_logout_revokes_only_that_session(client: AsyncClient) -> None:
    phone_session = await register(client, 55)
    tablet_session = (await login(client, 55)).json()

    response = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": phone_session["refresh_token"]},
        headers=bearer(phone_session["access_token"]),
    )
    assert response.status_code == 204
    assert error_code(await refresh(client, phone_session["refresh_token"])) == "TOKEN_REUSED"
    assert (await refresh(client, tablet_session["refresh_token"])).status_code == 200


async def test_logout_with_another_users_access_token_does_nothing(client: AsyncClient) -> None:
    victim = await register(client, 56)
    attacker = await register(client, 57)
    response = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": victim["refresh_token"]},
        headers=bearer(attacker["access_token"]),
    )
    assert response.status_code == 204
    assert (await refresh(client, victim["refresh_token"])).status_code == 200


async def test_logout_unknown_token_is_204(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/logout", json={"refresh_token": "nope"})
    assert response.status_code == 204


async def test_logout_works_with_expired_access_token(client: AsyncClient) -> None:
    data = await register(client, 58)
    expired_access = security.create_access_token(data["user"]["id"], "customer", None, lifetime=timedelta(seconds=-5))
    response = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": data["refresh_token"]}, headers=bearer(expired_access)
    )
    assert response.status_code == 204
    assert error_code(await refresh(client, data["refresh_token"])) == "TOKEN_REUSED"


async def test_logout_all(client: AsyncClient, db_session: AsyncSession) -> None:
    first = await register(client, 59)
    second = (await login(client, 59)).json()

    response = await client.post("/api/v1/auth/logout-all", headers=bearer(first["access_token"]))
    assert response.status_code == 204
    for session in (first, second):
        assert (await refresh(client, session["refresh_token"])).status_code == 401
    reasons = {t.revoked_reason for t in await tokens_of(db_session, first["user"]["id"])}
    assert reasons == {RevokeReason.logout_all}


async def test_access_token_errors(client: AsyncClient) -> None:
    missing = await client.get("/api/v1/users/me")
    assert missing.status_code == 401
    assert error_code(missing) == "NOT_AUTHENTICATED"

    garbage = await client.get("/api/v1/users/me", headers=bearer("garbage"))
    assert garbage.status_code == 401
    assert error_code(garbage) == "TOKEN_INVALID"

    data = await register(client, 60)
    expired = security.create_access_token(data["user"]["id"], "customer", None, lifetime=timedelta(seconds=-5))
    response = await client.get("/api/v1/users/me", headers=bearer(expired))
    assert response.status_code == 401
    assert error_code(response) == "TOKEN_EXPIRED"
    assert response.headers["WWW-Authenticate"] == "Bearer"

    ghost = security.create_access_token(999_999_999, "customer", None)
    response = await client.get("/api/v1/users/me", headers=bearer(ghost))
    assert response.status_code == 401
    assert error_code(response) == "USER_NOT_FOUND"
