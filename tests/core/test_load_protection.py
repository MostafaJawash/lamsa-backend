"""Password hashing under load, database pool exhaustion, and the request middleware."""

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core import security
from app.core.config import settings
from app.db.session import CONNECT_ARGS, get_db
from app.main import app
from app.modules.users.models import User
from app.shared.exceptions import ServiceBusy
from conftest import PASSWORD, error_code, login, phone, register

pytestmark = pytest.mark.anyio

# A cheap endpoint on the test session (/health would use the app's global engine pool).
BANNERS_URL = "/api/v1/banners"


# ------------------------------------------------------------------------------ hashing


def test_production_parameters_are_the_owasp_baseline() -> None:
    header = security.build_password_hasher().hash("x").split("$")[3]
    assert header == "m=19456,t=2,p=1"


async def test_limiter_caps_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PASSWORD_HASH_CONCURRENCY", 2)
    security.reset_hash_limiter()
    running = peak = 0
    lock = threading.Lock()

    def slow_hash(password: str) -> str:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return "hash"

    monkeypatch.setattr(security, "hash_password", slow_hash)
    assert await asyncio.gather(*[security.hash_password_async("p") for _ in range(8)]) == ["hash"] * 8
    assert peak == 2


async def test_queue_timeout_raises_service_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PASSWORD_HASH_CONCURRENCY", 1)
    monkeypatch.setattr(settings, "PASSWORD_HASH_QUEUE_TIMEOUT", 0.05)
    security.reset_hash_limiter()
    monkeypatch.setattr(security, "verify_password", lambda p, h: (time.sleep(0.3), (False, None))[1])
    results = await asyncio.gather(
        security.verify_password_async("a", "h"), security.verify_password_async("b", "h"), return_exceptions=True
    )
    assert results[0] == (False, None) and isinstance(results[1], ServiceBusy)
    assert results[1].status_code == 503 and results[1].headers == {"Retry-After": "2"}


@pytest.fixture
async def full_limiter(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Every hashing slot taken (as during a login burst)."""
    monkeypatch.setattr(settings, "PASSWORD_HASH_CONCURRENCY", 1)
    monkeypatch.setattr(settings, "PASSWORD_HASH_QUEUE_TIMEOUT", 0.05)
    security.reset_hash_limiter()
    limiter = security._limiter()
    await limiter.acquire()
    yield
    limiter.release()


async def test_login_and_dummy_verify_shed_load(client: AsyncClient, full_limiter: None) -> None:
    for n in (5, 99):  # an existing account would need a verify; an unknown phone the dummy verify
        response = await login(client, n)
        assert response.status_code == 503 and error_code(response) == "SERVICE_BUSY"
        assert response.headers["retry-after"] == "2" and response.json()["error"]["details"] == {"retry_after": 2}


async def test_register_sheds_load_too(client: AsyncClient, full_limiter: None) -> None:
    response = await client.post("/api/v1/auth/register", json={"name": "X", "phone": phone(7), "password": PASSWORD})
    assert response.status_code == 503 and error_code(response) == "SERVICE_BUSY"


async def test_old_parameter_hashes_verify_and_are_upgraded(client: AsyncClient, db_session: AsyncSession) -> None:
    user_id = (await register(client, 8))["user"]["id"]
    old = PasswordHash((Argon2Hasher(memory_cost=2048, time_cost=3, parallelism=2),)).hash(PASSWORD)
    await db_session.execute(update(User).where(User.id == user_id).values(password_hash=old))
    await db_session.commit()
    assert (await login(client, 8)).status_code == 200
    stored = await db_session.scalar(select(User.password_hash).where(User.id == user_id).execution_options(populate_existing=True))
    assert stored != old and stored is not None and "m=2048,t=3,p=2" not in stored
    assert (await login(client, 8)).status_code == 200  # the new hash works


# ------------------------------------------------------------------------- pool timeout


async def test_pool_exhaustion_returns_service_busy(client: AsyncClient) -> None:
    """A real pool of one connection, held elsewhere: the request waits DB_POOL_TIMEOUT, then 503."""
    engine = create_async_engine(settings.DATABASE_URL, pool_size=1, max_overflow=0, pool_timeout=0.2, connect_args=CONNECT_ARGS)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def tiny_pool_session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    previous = app.dependency_overrides[get_db]
    app.dependency_overrides[get_db] = tiny_pool_session
    try:
        async with engine.connect():  # the only connection is busy
            response = await client.get("/api/v1/categories")
        assert response.status_code == 503 and error_code(response) == "SERVICE_BUSY"
        assert response.headers["retry-after"] == "2"
        assert (await client.get("/api/v1/categories")).status_code == 200  # free again
    finally:
        app.dependency_overrides[get_db] = previous
        await engine.dispose()


# --------------------------------------------------------------------------- middleware


async def test_request_id_echo_and_generation(client: AsyncClient) -> None:
    echoed = await client.get(BANNERS_URL, headers={"X-Request-ID": "abc-123.x_y"})
    assert echoed.headers["x-request-id"] == "abc-123.x_y"
    generated = await client.get(BANNERS_URL)
    assert len(generated.headers["x-request-id"]) == 32
    bad = await client.get(BANNERS_URL, headers={"X-Request-ID": "has spaces & <script>"})
    assert bad.headers["x-request-id"] != "has spaces & <script>" and len(bad.headers["x-request-id"]) == 32


async def test_access_log_line(client: AsyncClient, base_categories: dict, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.access"):
        await client.get("/api/v1/categories/makeup", headers={"X-Request-ID": "req-1"})
        await client.get("/api/v1/categories/makeup", headers={"X-Request-ID": "req-2"})
    lines = [json.loads(r.message) for r in caplog.records if r.name == "app.access"]
    first, second = lines[-2:]
    assert first["path"] == "/api/v1/categories/{slug}" and first["status"] == 200 and first["request_id"] == "req-1"
    assert first["db_statements"] > 0 and first["cache"]["miss"] == 1
    assert second["db_statements"] == 0 and second["cache"]["hit"] == 1


async def test_server_timing_only_in_development(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "server-timing" not in (await client.get(BANNERS_URL)).headers
    monkeypatch.setattr(settings, "APP_ENV", "development")
    timing = (await client.get(BANNERS_URL)).headers["server-timing"]
    assert timing.startswith("app;dur=") and ", db;dur=" in timing


async def test_slow_query_and_slow_request_warnings(
    client: AsyncClient, base_categories: dict, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "SLOW_QUERY_MS", 0)
    monkeypatch.setattr(settings, "SLOW_REQUEST_MS", 0)
    with caplog.at_level(logging.WARNING, logger="app.slow"):
        await client.get("/api/v1/categories", headers={"X-Request-ID": "slow-1"})
    messages = [r.getMessage() for r in caplog.records if r.name == "app.slow"]
    assert any(m.startswith("Slow SQL") and "slow-1" in m for m in messages)
    assert any(m.startswith("Slow request") and '"request_id":"slow-1"' in m for m in messages)


async def test_login_releases_its_db_connection_while_hashing(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queued logins must not hold pooled connections (a login burst would starve everyone)."""
    await register(client, 9)
    seen: list[bool] = []
    real_verify = security.verify_password_async

    async def spy(password: str, password_hash: str) -> tuple[bool, str | None]:
        seen.append(db_session.in_transaction())
        return await real_verify(password, password_hash)

    monkeypatch.setattr(security, "verify_password_async", spy)
    assert (await login(client, 9)).status_code == 200
    assert seen == [False]
