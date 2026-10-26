from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.config import settings
from app.modules.auth.service import MAX_FAILED_PER_IP, MAX_FAILED_PER_PHONE
from app.modules.users.models import User
from conftest import (
    PASSWORD,
    ClientFactory,
    FakeClock,
    bearer,
    error_code,
    login,
    phone,
    register,
)

pytestmark = pytest.mark.anyio


# ----------------------------------------------------------------------------- register


async def test_register_success(client: AsyncClient) -> None:
    data = await register(client, 1, name="  ليلى  ")

    assert data["token_type"] == "bearer"
    assert data["expires_in"] == 15 * 60
    assert data["refresh_expires_in"] == 60 * 24 * 60 * 60
    assert data["access_token"] and data["refresh_token"]
    user = data["user"]
    assert user["name"] == "ليلى"
    assert user["phone"] == phone(1)
    assert user["role"] == "customer"
    assert user["auth_provider"] == "phone"
    assert user["must_change_password"] is False
    assert user["created_at"]

    me = await client.get("/api/v1/users/me", headers=bearer(data["access_token"]))
    assert me.status_code == 200
    assert me.json()["id"] == user["id"]


async def test_register_stores_argon2_hash_and_last_login(client: AsyncClient, db_session: AsyncSession) -> None:
    data = await register(client, 1)
    user = await db_session.get(User, data["user"]["id"])
    assert user is not None
    assert user.password_hash and user.password_hash.startswith("$argon2id$")
    assert PASSWORD not in user.password_hash
    assert user.last_login_at is not None
    assert user.google_sub is None


async def test_register_phone_taken(client: AsyncClient) -> None:
    await register(client, 1)
    response = await client.post(
        "/api/v1/auth/register", json={"name": "Other", "phone": "0151 234567 01", "password": PASSWORD}
    )
    assert response.status_code == 409
    assert error_code(response) == "PHONE_TAKEN"


@pytest.mark.parametrize(
    "raw",
    ["015123456702", "0151 2345 6702", "+49 151 23456702", "0049 151 23456702", "+49-151-234-567-02", "(0151) 23456702"],
)
async def test_register_normalizes_phone_formats(client: AsyncClient, raw: str) -> None:
    response = await client.post("/api/v1/auth/register", json={"name": "A", "phone": raw, "password": PASSWORD})
    assert response.status_code == 201, response.text
    assert response.json()["user"]["phone"] == phone(2)


@pytest.mark.parametrize("raw", ["123", "abc", "+49 151", "0000000000", "+999 1234567"])
async def test_register_rejects_invalid_phone(client: AsyncClient, raw: str) -> None:
    response = await client.post("/api/v1/auth/register", json={"name": "A", "phone": raw, "password": PASSWORD})
    assert response.status_code == 422
    assert error_code(response) == "INVALID_PHONE"


@pytest.mark.parametrize(
    ("password", "fragment"),
    [
        ("short1", "8"),
        ("x" * 129, "128"),
        ("1234567890", "أرقام"),
        ("+4915123456703", "رقم الهاتف"),
        ("0151 2345 6703", "رقم الهاتف"),
    ],
)
async def test_register_rejects_weak_passwords(client: AsyncClient, password: str, fragment: str) -> None:
    response = await client.post(
        "/api/v1/auth/register", json={"name": "A", "phone": phone(3), "password": password}
    )
    assert response.status_code == 422
    assert error_code(response) == "WEAK_PASSWORD"
    assert fragment in response.json()["error"]["message"]


async def test_register_race_on_unique_phone_maps_to_conflict(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await register(client, 4)

    async def never_taken(*args: object, **kwargs: object) -> bool:
        return False  # simulate the pre-check losing a race

    monkeypatch.setattr("app.modules.users.repository.phone_taken", never_taken)
    response = await client.post(
        "/api/v1/auth/register", json={"name": "B", "phone": phone(4), "password": PASSWORD}
    )
    assert response.status_code == 409
    assert error_code(response) == "PHONE_TAKEN"


# -------------------------------------------------------------------------------- login


async def test_login_success_updates_last_login(client: AsyncClient, db_session: AsyncSession, frozen_clock: FakeClock) -> None:
    data = await register(client, 5)
    frozen_clock.advance(hours=1)

    response = await client.post(
        "/api/v1/auth/login", json={"phone": "0151 2345 6705", "password": PASSWORD, "device_name": "Pixel 8"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["user"]["id"] == data["user"]["id"]
    user = await db_session.get(User, data["user"]["id"])
    assert user is not None and user.last_login_at == frozen_clock.now


async def test_login_rehashes_outdated_hash(client: AsyncClient, db_session: AsyncSession) -> None:
    data = await register(client, 6)
    from pwdlib import PasswordHash
    from pwdlib.hashers.argon2 import Argon2Hasher

    old_hash = PasswordHash((Argon2Hasher(time_cost=2, memory_cost=1024, parallelism=1),)).hash(PASSWORD)
    user = await db_session.get(User, data["user"]["id"])
    assert user is not None
    user.password_hash = old_hash
    await db_session.commit()

    assert (await login(client, 6)).status_code == 200

    refreshed = await db_session.scalar(
        select(User).where(User.id == user.id).execution_options(populate_existing=True)
    )
    assert refreshed is not None and refreshed.password_hash != old_hash
    assert security.verify_password(PASSWORD, refreshed.password_hash)[0]


async def test_login_errors_are_identical(client: AsyncClient, google: dict, db_session: AsyncSession) -> None:
    await register(client, 7)
    # A Google account (no password) with a known phone.
    from app.integrations.google.auth import GoogleIdentity

    google["g"] = GoogleIdentity(sub="g-sub-7", email="g@example.com", name="G")
    signup = (await client.post("/api/v1/auth/google", json={"id_token": "g"})).json()["signup_token"]
    done = await client.post(
        "/api/v1/auth/google/complete", json={"signup_token": signup, "name": "G", "phone": phone(8)}
    )
    assert done.status_code == 201

    wrong_password = await login(client, 7, "Wrong-pass1")
    unknown_phone = await login(client, 9)
    google_by_phone = await login(client, 8)

    for response in (wrong_password, unknown_phone, google_by_phone):
        assert response.status_code == 401
        assert response.json() == wrong_password.json()
        assert error_code(response) == "INVALID_CREDENTIALS"


async def test_login_rate_limit_per_phone(client_factory: ClientFactory, frozen_clock: FakeClock) -> None:
    first = await client_factory("10.1.0.1")
    await register(first, 10)

    for i in range(MAX_FAILED_PER_PHONE):
        other_ip_client = await client_factory(f"10.1.1.{i}")
        assert (await login(other_ip_client, 10, "Wrong-pass1")).status_code == 401

    blocked = await login(await client_factory("10.1.2.1"), 10, PASSWORD)  # even the right password
    assert blocked.status_code == 429
    assert error_code(blocked) == "TOO_MANY_ATTEMPTS"
    retry_after = int(blocked.headers["Retry-After"])
    assert 0 < retry_after <= 15 * 60
    assert blocked.json()["error"]["details"]["retry_after"] == retry_after

    frozen_clock.advance(minutes=15, seconds=1)
    assert (await login(first, 10, PASSWORD)).status_code == 200


async def test_login_rate_limit_per_ip(client_factory: ClientFactory, frozen_clock: FakeClock) -> None:
    attacker = await client_factory("10.2.0.1")
    for i in range(MAX_FAILED_PER_IP):
        response = await attacker.post("/api/v1/auth/login", json={"phone": phone(20 + i % 4), "password": "Wrong-pass1"})
        assert response.status_code == 401

    blocked = await login(attacker, 30, "Wrong-pass1")
    assert blocked.status_code == 429
    assert error_code(blocked) == "TOO_MANY_ATTEMPTS"

    # Another IP is not affected.
    assert (await login(await client_factory("10.2.0.2"), 30, "Wrong-pass1")).status_code == 401


async def test_login_rate_limit_counts_forwarded_ip_from_trusted_proxy(
    client_factory: ClientFactory, frozen_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behind the website server (trusted), each visitor has their own IP bucket; a direct
    client spoofing X-Forwarded-For still counts as its socket address."""
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", ["10.9.0.1"])
    website = await client_factory("10.9.0.1")
    for i in range(MAX_FAILED_PER_IP):
        response = await website.post(
            "/api/v1/auth/login",
            json={"phone": phone(40 + i % 4), "password": "Wrong-pass1"},
            headers={"X-Forwarded-For": "198.51.100.1"},
        )
        assert response.status_code == 401
    blocked = await website.post(
        "/api/v1/auth/login", json={"phone": phone(45), "password": "Wrong-pass1"},
        headers={"X-Forwarded-For": "198.51.100.1"},
    )
    assert error_code(blocked) == "TOO_MANY_ATTEMPTS"
    other_visitor = await website.post(
        "/api/v1/auth/login", json={"phone": phone(45), "password": "Wrong-pass1"},
        headers={"X-Forwarded-For": "198.51.100.2"},
    )
    assert other_visitor.status_code == 401

    spoofer = await client_factory("10.9.0.99")
    for i in range(MAX_FAILED_PER_IP):
        await spoofer.post(
            "/api/v1/auth/login", json={"phone": phone(50 + i % 4), "password": "Wrong-pass1"},
            headers={"X-Forwarded-For": f"198.51.100.{100 + i}"},
        )
    assert error_code(await login(spoofer, 55, "Wrong-pass1")) == "TOO_MANY_ATTEMPTS"


async def test_successful_logins_do_not_count(client_factory: ClientFactory) -> None:
    c = await client_factory("10.3.0.1")
    await register(c, 31)
    for _ in range(MAX_FAILED_PER_PHONE + 2):
        assert (await login(c, 31)).status_code == 200


async def test_login_disabled_account(client: AsyncClient, db_session: AsyncSession) -> None:
    data = await register(client, 32)
    user = await db_session.get(User, data["user"]["id"])
    assert user is not None
    user.is_active = False
    await db_session.commit()

    assert error_code(await login(client, 32, "Wrong-pass1")) == "INVALID_CREDENTIALS"
    response = await login(client, 32)
    assert response.status_code == 403
    assert error_code(response) == "ACCOUNT_DISABLED"


async def test_login_invalid_phone_format(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/login", json={"phone": "12", "password": PASSWORD})
    assert response.status_code == 422
    assert error_code(response) == "INVALID_PHONE"


async def test_cleanup_deletes_attempts_older_than_7_days(db_session: AsyncSession, frozen_clock: FakeClock) -> None:
    from sqlalchemy import func

    from app.modules.auth import repository as auth_repo
    from app.modules.auth.models import LoginAttempt
    from app.modules.auth.service import cleanup_login_attempts

    now = frozen_clock.now
    auth_repo.add_login_attempt(db_session, phone(33), "10.4.0.1", False, now - timedelta(days=8))
    auth_repo.add_login_attempt(db_session, phone(33), "10.4.0.1", False, now - timedelta(days=6))
    await db_session.flush()

    assert await cleanup_login_attempts(db_session, now) == 1
    remaining = await db_session.scalar(select(func.count()).select_from(LoginAttempt).where(LoginAttempt.phone == phone(33)))
    assert remaining == 1
