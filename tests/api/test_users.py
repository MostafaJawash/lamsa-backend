from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.google.auth import GoogleIdentity
from app.modules.auth.models import RefreshToken
from app.modules.orders.models import Order
from app.modules.users.models import User, UserAddress, UserDevice
from conftest import PASSWORD, bearer, error_code, phone, register

pytestmark = pytest.mark.anyio


async def google_user(client: AsyncClient, google: dict, n: int) -> dict:
    google[f"g{n}"] = GoogleIdentity(sub=f"g-sub-{n}", email=f"g{n}@example.com", name="G")
    signup = (await client.post("/api/v1/auth/google", json={"id_token": f"g{n}"})).json()["signup_token"]
    response = await client.post(
        "/api/v1/auth/google/complete", json={"signup_token": signup, "name": "G", "phone": phone(n)}
    )
    assert response.status_code == 201
    data: dict = response.json()
    return data


async def count(session: AsyncSession, model: type, user_id: int) -> int:
    return int(await session.scalar(select(func.count()).select_from(model).where(model.user_id == user_id)) or 0)


# ---------------------------------------------------------------------------------- profile


async def test_update_profile(client: AsyncClient) -> None:
    data = await register(client, 71)
    headers = bearer(data["access_token"])

    response = await client.patch("/api/v1/users/me", json={"name": "  سارة  ", "email": "Sara@Example.com"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["name"] == "سارة"
    assert response.json()["email"] == "Sara@example.com"

    cleared = await client.patch("/api/v1/users/me", json={"email": None}, headers=headers)
    assert cleared.json()["email"] is None
    assert cleared.json()["name"] == "سارة"

    invalid = await client.patch("/api/v1/users/me", json={"email": "not-an-email"}, headers=headers)
    assert invalid.status_code == 422 and error_code(invalid) == "VALIDATION_ERROR"


# ------------------------------------------------------------------------------ phone change


async def test_phone_account_changes_phone_with_password(client: AsyncClient) -> None:
    data = await register(client, 72)
    headers = bearer(data["access_token"])

    missing = await client.post("/api/v1/users/me/phone", json={"phone": phone(73)}, headers=headers)
    assert missing.status_code == 422 and error_code(missing) == "CURRENT_PASSWORD_REQUIRED"

    wrong = await client.post(
        "/api/v1/users/me/phone", json={"phone": phone(73), "current_password": "Wrong-pass1"}, headers=headers
    )
    assert wrong.status_code == 401 and error_code(wrong) == "INVALID_CREDENTIALS"

    ok = await client.post(
        "/api/v1/users/me/phone", json={"phone": "0151 2345 6773", "current_password": PASSWORD}, headers=headers
    )
    assert ok.status_code == 200 and ok.json()["phone"] == phone(73)

    unchanged = await client.post(
        "/api/v1/users/me/phone", json={"phone": phone(73), "current_password": PASSWORD}, headers=headers
    )
    assert unchanged.status_code == 200 and unchanged.json()["phone"] == phone(73)


async def test_phone_change_taken(client: AsyncClient) -> None:
    await register(client, 74)
    data = await register(client, 75)
    response = await client.post(
        "/api/v1/users/me/phone",
        json={"phone": phone(74), "current_password": PASSWORD},
        headers=bearer(data["access_token"]),
    )
    assert response.status_code == 409 and error_code(response) == "PHONE_TAKEN"


async def test_google_account_changes_phone_without_password(client: AsyncClient, google: dict) -> None:
    data = await google_user(client, google, 76)
    response = await client.post(
        "/api/v1/users/me/phone", json={"phone": phone(77)}, headers=bearer(data["access_token"])
    )
    assert response.status_code == 200 and response.json()["phone"] == phone(77)


# ------------------------------------------------------------------------------ delete account


async def test_delete_account_without_orders(client: AsyncClient, db_session: AsyncSession) -> None:
    data = await register(client, 78)
    headers = bearer(data["access_token"])
    user_id = data["user"]["id"]
    await client.post("/api/v1/users/me/addresses", json={"city": "Berlin", "street": "Str 1"}, headers=headers)
    await client.post("/api/v1/users/me/devices", json={"fcm_token": "tok-78", "platform": "android"}, headers=headers)
    # A rotated chain of refresh tokens (self-referencing FK) must cascade cleanly too.
    await client.post("/api/v1/auth/refresh", json={"refresh_token": data["refresh_token"]})

    missing = await client.request("DELETE", "/api/v1/users/me", headers=headers)
    assert missing.status_code == 422 and error_code(missing) == "CURRENT_PASSWORD_REQUIRED"
    wrong = await client.request("DELETE", "/api/v1/users/me", json={"current_password": "Wrong-pass1"}, headers=headers)
    assert wrong.status_code == 401

    response = await client.request("DELETE", "/api/v1/users/me", json={"current_password": PASSWORD}, headers=headers)
    assert response.status_code == 204

    assert await db_session.get(User, user_id) is None
    for model in (UserAddress, UserDevice, RefreshToken):
        assert await count(db_session, model, user_id) == 0
    after = await client.get("/api/v1/users/me", headers=headers)
    assert after.status_code == 401 and error_code(after) == "USER_NOT_FOUND"


async def test_delete_account_with_orders_is_anonymised(client: AsyncClient, db_session: AsyncSession) -> None:
    data = await register(client, 79)
    headers = bearer(data["access_token"])
    user_id = data["user"]["id"]
    await client.post("/api/v1/users/me/addresses", json={"city": "Berlin", "street": "Str 1"}, headers=headers)
    await client.post("/api/v1/users/me/devices", json={"fcm_token": "tok-79", "platform": "ios"}, headers=headers)
    # Insert an order directly (lamsa_test only, inside the rolled-back test transaction).
    db_session.add(
        Order(
            user_id=user_id, customer_name="Test User", customer_phone=phone(79),
            delivery_method_code="home", delivery_method_name="توصيل للبيت",
            subtotal=Decimal("10.00"), shipping_fee=Decimal("4.95"), total=Decimal("14.95"),
        )
    )
    await db_session.commit()
    db_session.expunge_all()

    response = await client.request("DELETE", "/api/v1/users/me", json={"current_password": PASSWORD}, headers=headers)
    assert response.status_code == 204

    user = await db_session.scalar(select(User).where(User.id == user_id).execution_options(populate_existing=True))
    assert user is not None
    assert user.name == "حساب محذوف"
    assert user.phone == f"deleted-{user_id}"
    assert user.email is None and user.google_sub is None and user.password_hash is None
    assert user.is_active is False
    for model in (UserAddress, UserDevice, RefreshToken):
        assert await count(db_session, model, user_id) == 0
    assert await count(db_session, Order, user_id) == 1

    # The phone is free again for a new account.
    assert (await client.post(
        "/api/v1/auth/register", json={"name": "New", "phone": phone(79), "password": PASSWORD}
    )).status_code == 201


async def test_google_account_deletes_without_password(client: AsyncClient, google: dict) -> None:
    data = await google_user(client, google, 80)
    response = await client.request("DELETE", "/api/v1/users/me", headers=bearer(data["access_token"]))
    assert response.status_code == 204
