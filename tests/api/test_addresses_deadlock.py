"""Address writes survive InnoDB deadlocks (1213) like cart writes and checkout do.

Found by the website's parallel e2e run: concurrent `POST /users/me/addresses` of different users
sometimes deadlocked on the INSERT and answered 500 INTERNAL_ERROR. Now the transaction is retried
(up to 3 attempts in all) and a persistent deadlock is 503 SERVICE_BUSY, never a 500.

Two kinds of test: deterministic ones that inject a deadlock into the user lock every address write
takes (inside the usual rolled-back transaction), and one stress test on real connections with real
commits (lamsa_test; wiped afterwards) that fires many address writes of different users at once.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pymysql.err import OperationalError as MySQLOperationalError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db.session import CONNECT_ARGS, get_db
from app.main import app
from app.modules.users.service import AddressService
from conftest import bearer, error_code, register

pytestmark = pytest.mark.anyio
BASE = "/api/v1/users/me/addresses"
BODY = {"governorate": "damascus", "details": "شارع الجلاء، بناء 12، الطابق 3"}


async def headers(client: AsyncClient, n: int) -> dict[str, str]:
    return bearer((await register(client, n))["access_token"])


def deadlocking(failures: int) -> tuple[Any, list[int]]:
    """`AddressService._lock_user` failing with a deadlock the first `failures` times."""
    real = AddressService._lock_user
    calls: list[int] = []

    async def wrapper(self: AddressService, user_id: int) -> None:
        calls.append(1)
        if len(calls) <= failures:
            raise OperationalError("SELECT ... FOR UPDATE", {}, MySQLOperationalError(1213, "Deadlock found"))
        await real(self, user_id)

    return wrapper, calls


async def test_a_deadlock_on_create_is_retried(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    h = await headers(client, 1)
    wrapper, calls = deadlocking(failures=1)
    monkeypatch.setattr(AddressService, "_lock_user", wrapper)
    response = await client.post(BASE, json=BODY, headers=h)
    assert response.status_code == 201 and len(calls) == 2
    assert response.json()["is_default"] is True
    monkeypatch.undo()
    assert len((await client.get(BASE, headers=h)).json()) == 1  # created once, not twice


async def test_two_deadlocks_still_succeed(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    h = await headers(client, 2)
    wrapper, calls = deadlocking(failures=2)
    monkeypatch.setattr(AddressService, "_lock_user", wrapper)
    assert (await client.post(BASE, json=BODY, headers=h)).status_code == 201 and len(calls) == 3


async def test_a_persistent_deadlock_is_service_busy_and_leaves_nothing_half_done(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = await headers(client, 3)
    wrapper, calls = deadlocking(failures=99)
    monkeypatch.setattr(AddressService, "_lock_user", wrapper)
    response = await client.post(BASE, json=BODY, headers=h)
    assert response.status_code == 503 and error_code(response) == "SERVICE_BUSY"
    assert response.headers["retry-after"] == "1" and len(calls) == 3  # three attempts, then shed load
    monkeypatch.undo()
    assert (await client.get(BASE, headers=h)).json() == []
    assert (await client.post(BASE, json=BODY, headers=h)).status_code == 201


async def test_update_default_and_delete_are_retried_too(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    h = await headers(client, 4)
    first = (await client.post(BASE, json=BODY, headers=h)).json()
    second = (await client.post(BASE, json={**BODY, "details": "شارع بغداد، بناء 4، الطابق 1"}, headers=h)).json()

    wrapper, calls = deadlocking(failures=1)
    monkeypatch.setattr(AddressService, "_lock_user", wrapper)
    patched = await client.patch(f"{BASE}/{second['id']}", json={"area": "المزة"}, headers=h)
    assert patched.status_code == 200 and patched.json()["area"] == "المزة" and len(calls) == 2

    wrapper, calls = deadlocking(failures=1)
    monkeypatch.setattr(AddressService, "_lock_user", wrapper)
    made_default = await client.post(f"{BASE}/{second['id']}/default", headers=h)
    assert made_default.status_code == 200 and made_default.json()["is_default"] is True and len(calls) == 2

    wrapper, calls = deadlocking(failures=1)
    monkeypatch.setattr(AddressService, "_lock_user", wrapper)
    assert (await client.delete(f"{BASE}/{second['id']}", headers=h)).status_code == 204 and len(calls) == 2
    monkeypatch.undo()
    remaining = (await client.get(BASE, headers=h)).json()
    assert [a["id"] for a in remaining] == [first["id"]] and remaining[0]["is_default"] is True  # the default moved on


async def test_real_errors_are_not_swallowed_by_the_retry(client: AsyncClient) -> None:
    h = await headers(client, 5)
    assert (await client.patch(f"{BASE}/999999", json={"area": "x"}, headers=h)).status_code == 404
    bad = await client.post(BASE, json={**BODY, "latitude": "52.5", "longitude": "13.4"}, headers=h)
    assert bad.status_code == 422 and error_code(bad) == "INVALID_LOCATION"


# ------------------------------------------------------------- real connections, real commits

CLEANUP = ["DELETE FROM user_addresses", "DELETE FROM users", "DELETE FROM login_attempts"]


@pytest.fixture
async def committed(migrated_db: None) -> AsyncIterator[dict[str, Any]]:
    assert settings.DB_NAME.endswith("_test")
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool, connect_args=CONNECT_ARGS)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def real_session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db] = real_session
    try:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            users = [bearer((await register(client, 20 + n, name=f"Buyer {n}"))["access_token"]) for n in range(12)]
            yield {"client": client, "users": users}
    finally:
        app.dependency_overrides.pop(get_db, None)
        async with engine.begin() as conn:
            for statement in CLEANUP:
                await conn.execute(text(statement))
        await engine.dispose()


async def test_parallel_address_writes_never_answer_500(committed: dict[str, Any]) -> None:
    """12 customers add, change and remove addresses at the same moment, many times over."""
    client: AsyncClient = committed["client"]
    users: list[dict[str, str]] = committed["users"]
    statuses: list[int] = []

    async def customer(h: dict[str, str]) -> None:
        for round_number in range(6):
            body = {**BODY, "details": f"شارع الجلاء، بناء {round_number}، الطابق 3", "is_default": round_number % 2 == 0}
            created = await client.post(BASE, json=body, headers=h)
            statuses.append(created.status_code)
            if created.status_code == 201:
                address_id = created.json()["id"]
                statuses.append((await client.post(f"{BASE}/{address_id}/default", headers=h)).status_code)
                statuses.append((await client.patch(f"{BASE}/{address_id}", json={"area": "المزة"}, headers=h)).status_code)
                if round_number % 2:
                    statuses.append((await client.delete(f"{BASE}/{address_id}", headers=h)).status_code)

    await asyncio.gather(*(customer(h) for h in users))
    assert statuses
    # Every request succeeded or was shed as 503 SERVICE_BUSY; never an unhandled 500.
    assert all(code in (200, 201, 204, 503) for code in statuses), sorted(set(statuses))
    assert statuses.count(500) == 0
    # Each customer still has a consistent address book: exactly one default.
    for h in users:
        book = (await client.get(BASE, headers=h)).json()
        assert book and sum(1 for a in book if a["is_default"]) == 1
