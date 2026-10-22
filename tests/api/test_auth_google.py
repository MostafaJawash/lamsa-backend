from datetime import timedelta

import pytest
from httpx import AsyncClient

from app.core import security
from app.integrations.google.auth import GoogleIdentity
from conftest import bearer, error_code, phone, register

pytestmark = pytest.mark.anyio

IDENTITY = GoogleIdentity(sub="google-sub-1", email="layla@example.com", name="Layla")


async def start(client: AsyncClient, token: str = "tok") -> dict:
    response = await client.post("/api/v1/auth/google", json={"id_token": token, "device_name": "iPhone"})
    assert response.status_code == 200, response.text
    return response.json()


async def complete(client: AsyncClient, signup_token: str, n: int, name: str = "ليلى") -> object:
    return await client.post(
        "/api/v1/auth/google/complete", json={"signup_token": signup_token, "name": name, "phone": phone(n)}
    )


async def test_new_user_gets_signup_token_then_completes(client: AsyncClient, google: dict) -> None:
    google["tok"] = IDENTITY

    first = await start(client)
    assert first["registered"] is False
    assert first["tokens"] is None
    assert first["profile"] == {"email": "layla@example.com", "name": "Layla"}
    assert first["signup_token"]

    response = await complete(client, first["signup_token"], 40)
    assert response.status_code == 201, response.text
    user = response.json()["user"]
    assert user["auth_provider"] == "google"
    assert user["email"] == "layla@example.com"
    assert user["name"] == "ليلى"
    assert user["phone"] == phone(40)


async def test_existing_user_gets_tokens(client: AsyncClient, google: dict) -> None:
    google["tok"] = IDENTITY
    first = await start(client)
    created = (await complete(client, first["signup_token"], 41)).json()

    again = await start(client)
    assert again["registered"] is True
    assert again["signup_token"] is None
    tokens = again["tokens"]
    assert tokens["user"]["id"] == created["user"]["id"]
    me = await client.get("/api/v1/users/me", headers=bearer(tokens["access_token"]))
    assert me.status_code == 200


async def test_invalid_google_token(client: AsyncClient, google: dict) -> None:
    response = await client.post("/api/v1/auth/google", json={"id_token": "not-known"})
    assert response.status_code == 401
    assert error_code(response) == "GOOGLE_TOKEN_INVALID"


async def test_expired_signup_token(client: AsyncClient) -> None:
    expired = security.create_signup_token("sub-x", "x@example.com", "X", lifetime=timedelta(seconds=-5))
    response = await complete(client, expired, 42)
    assert response.status_code == 401
    assert error_code(response) == "TOKEN_EXPIRED"


async def test_already_registered(client: AsyncClient, google: dict) -> None:
    google["tok"] = IDENTITY
    first = await start(client)
    second = await start(client)  # e.g. two devices started signup at the same time
    assert (await complete(client, first["signup_token"], 43)).status_code == 201

    response = await complete(client, second["signup_token"], 44)
    assert response.status_code == 409
    assert error_code(response) == "ALREADY_REGISTERED"


async def test_phone_taken_on_complete(client: AsyncClient, google: dict) -> None:
    await register(client, 45)
    google["tok"] = IDENTITY
    first = await start(client)
    response = await complete(client, first["signup_token"], 45)
    assert response.status_code == 409
    assert error_code(response) == "PHONE_TAKEN"


async def test_access_token_rejected_as_signup_token(client: AsyncClient) -> None:
    data = await register(client, 46)
    response = await complete(client, data["access_token"], 47)
    assert response.status_code == 401
    assert error_code(response) == "TOKEN_INVALID"


async def test_signup_token_rejected_as_access_token(client: AsyncClient, google: dict) -> None:
    google["tok"] = IDENTITY
    signup_token = (await start(client))["signup_token"]
    response = await client.get("/api/v1/users/me", headers=bearer(signup_token))
    assert response.status_code == 401
    assert error_code(response) == "TOKEN_INVALID"


async def test_disabled_google_user_cannot_sign_in(client: AsyncClient, google: dict, db_session) -> None:
    from app.modules.users.models import User

    google["tok"] = IDENTITY
    created = (await complete(client, (await start(client))["signup_token"], 48)).json()
    user = await db_session.get(User, created["user"]["id"])
    user.is_active = False
    await db_session.commit()

    response = await client.post("/api/v1/auth/google", json={"id_token": "tok"})
    assert response.status_code == 403
    assert error_code(response) == "ACCOUNT_DISABLED"
