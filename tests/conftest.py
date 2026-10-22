"""Test setup.

Safety: before anything from `app` is imported, DB_NAME is switched to TEST_DATABASE_NAME,
and the run is refused unless that name ends in "_test". Tests never touch the lamsa database.

Database tests: migrations run once per session; every test runs inside one outer transaction
that is rolled back at the end (services' commits become savepoint releases).
"""

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
_dotenv = dotenv_values(ROOT / ".env")
TEST_DB = os.environ.get("TEST_DATABASE_NAME") or _dotenv.get("TEST_DATABASE_NAME") or "lamsa_test"
if not TEST_DB.endswith("_test"):
    raise pytest.UsageError(f"Refusing to run: TEST_DATABASE_NAME={TEST_DB!r} must end with '_test'")

os.environ["DB_NAME"] = TEST_DB
os.environ["APP_ENV"] = "test"
os.environ["DEBUG"] = "false"
os.environ["GOOGLE_DEV_MODE"] = "false"
os.environ["TRUST_PROXY_HEADERS"] = "false"
os.environ["JWT_SECRET"] = "test-only-secret-" + "x" * 48

from httpx import ASGITransport, AsyncClient  # noqa: E402
from pwdlib import PasswordHash  # noqa: E402
from pwdlib.hashers.argon2 import Argon2Hasher  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core import clock, security  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.session import CONNECT_ARGS, get_db  # noqa: E402
from app.integrations.google import auth as google_auth  # noqa: E402
from app.integrations.google.auth import GoogleIdentity  # noqa: E402
from app.main import app  # noqa: E402
from app.modules.users.enums import UserRole  # noqa: E402
from app.modules.users.models import User  # noqa: E402
from app.shared.exceptions import ErrorCode, Unauthorized  # noqa: E402

assert settings.DB_NAME == TEST_DB and settings.DB_NAME.endswith("_test"), "tests must use the test database"

DEFAULT_IP = "10.0.0.1"
PASSWORD = "Secret-pass1"


def phone(n: int) -> str:
    """A valid German mobile number in E.164, distinct per n (0..99)."""
    return f"+49151234567{n:02d}"


# ----------------------------------------------------------------------------- fixtures


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
def fast_password_hashing() -> Iterator[None]:
    """Argon2 with production cost makes the suite slow; use cheap parameters in tests."""
    original = security.password_hasher
    security.password_hasher = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=1024, parallelism=1),))
    security._dummy_hash.cache_clear()
    yield
    security.password_hasher = original
    security._dummy_hash.cache_clear()


@pytest.fixture(autouse=True)
def no_real_google(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(token: str) -> dict[str, Any]:
        raise AssertionError("Tests must never call Google")

    monkeypatch.setattr(google_auth, "_verify_with_google", refuse)


@pytest.fixture(scope="session")
def migrated_db() -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")


@pytest.fixture
async def db_session(migrated_db: None) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool, connect_args=CONNECT_ARGS)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


ClientFactory = Callable[..., Awaitable[AsyncClient]]


@pytest.fixture
async def client_factory(db_session: AsyncSession) -> AsyncIterator[ClientFactory]:
    """Create clients with a chosen source IP. All share the test transaction."""

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session
        # Like production (one session per request): forget loaded objects between requests.
        db_session.expunge_all()

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncExitStack() as stack:

        async def make(ip: str = DEFAULT_IP) -> AsyncClient:
            transport = ASGITransport(app=app, raise_app_exceptions=False, client=(ip, 50000))
            return await stack.enter_async_context(AsyncClient(transport=transport, base_url="http://test"))

        yield make
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
async def client(client_factory: ClientFactory) -> AsyncClient:
    return await client_factory()


@dataclass
class FakeClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock(clock.utcnow())
    monkeypatch.setattr(clock, "utcnow", fake)
    return fake


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> dict[str, GoogleIdentity]:
    """Map of id_token -> identity accepted by the mocked Google verification."""
    identities: dict[str, GoogleIdentity] = {}

    async def fake_verify(token: str) -> GoogleIdentity:
        if token in identities:
            return identities[token]
        raise Unauthorized(ErrorCode.GOOGLE_TOKEN_INVALID)

    monkeypatch.setattr(google_auth, "verify_google_id_token", fake_verify)
    return identities


# ------------------------------------------------------------------------------ helpers


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def register(
    client: AsyncClient, n: int = 1, password: str = PASSWORD, name: str = "Test User"
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/auth/register", json={"name": name, "phone": phone(n), "password": password}
    )
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    return data


async def login(client: AsyncClient, n: int = 1, password: str = PASSWORD) -> Any:
    return await client.post("/api/v1/auth/login", json={"phone": phone(n), "password": password})


async def make_admin(session: AsyncSession, user_id: int) -> None:
    user = await session.scalar(select(User).where(User.id == user_id).execution_options(populate_existing=True))
    assert user is not None
    user.role = UserRole.admin
    await session.commit()


def error_code(response: Any) -> str:
    body = response.json()
    assert set(body) == {"error"}, body
    assert set(body["error"]) == {"code", "message", "details"}, body
    code: str = body["error"]["code"]
    return code
