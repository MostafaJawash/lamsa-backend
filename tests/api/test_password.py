import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.google.auth import GoogleIdentity
from conftest import PASSWORD, bearer, error_code, login, make_admin, phone, register

pytestmark = pytest.mark.anyio

NEW_PASSWORD = "Brand-new-pass2"


async def change(client: AsyncClient, access: str, current: str, new: str) -> object:
    return await client.post(
        "/api/v1/auth/password/change",
        json={"current_password": current, "new_password": new},
        headers=bearer(access),
    )


async def refresh_status(client: AsyncClient, token: str) -> int:
    return (await client.post("/api/v1/auth/refresh", json={"refresh_token": token})).status_code


async def test_change_password_wrong_current(client: AsyncClient) -> None:
    data = await register(client, 61)
    response = await change(client, data["access_token"], "Wrong-pass1", NEW_PASSWORD)
    assert response.status_code == 401
    assert error_code(response) == "INVALID_CREDENTIALS"


async def test_change_password_must_differ_and_follow_rules(client: AsyncClient) -> None:
    data = await register(client, 62)
    same = await change(client, data["access_token"], PASSWORD, PASSWORD)
    assert same.status_code == 422 and error_code(same) == "SAME_PASSWORD"
    weak = await change(client, data["access_token"], PASSWORD, "12345678")
    assert weak.status_code == 422 and error_code(weak) == "WEAK_PASSWORD"


async def test_change_password_revokes_other_sessions(client: AsyncClient) -> None:
    this_device = await register(client, 63)
    other_device = (await login(client, 63)).json()

    response = await change(client, this_device["access_token"], PASSWORD, NEW_PASSWORD)
    assert response.status_code == 200, response.text
    fresh = response.json()

    assert await refresh_status(client, other_device["refresh_token"]) == 401
    assert await refresh_status(client, fresh["refresh_token"]) == 200
    assert (await login(client, 63, PASSWORD)).status_code == 401
    assert (await login(client, 63, NEW_PASSWORD)).status_code == 200


async def test_google_account_cannot_change_password(client: AsyncClient, google: dict) -> None:
    google["g"] = GoogleIdentity(sub="g-64", email=None, name=None)
    signup = (await client.post("/api/v1/auth/google", json={"id_token": "g"})).json()["signup_token"]
    data = (
        await client.post(
            "/api/v1/auth/google/complete", json={"signup_token": signup, "name": "G", "phone": phone(64)}
        )
    ).json()
    response = await change(client, data["access_token"], PASSWORD, NEW_PASSWORD)
    assert response.status_code == 422
    assert error_code(response) == "NOT_A_PASSWORD_ACCOUNT"


async def test_admin_reset_password_end_to_end(client: AsyncClient, db_session: AsyncSession) -> None:
    admin = await register(client, 65, name="Admin")
    await make_admin(db_session, admin["user"]["id"])
    customer = await register(client, 66, name="Customer")

    # Customer forgot the password and calls the store.
    reset = await client.post(
        f"/api/v1/admin/users/{customer['user']['id']}/reset-password", headers=bearer(admin["access_token"])
    )
    assert reset.status_code == 200, reset.text
    temporary = reset.json()["temporary_password"]
    assert len(temporary) == 10
    assert not set(temporary) & set("0O1lIi")

    # Old sessions are gone, old password no longer works.
    assert await refresh_status(client, customer["refresh_token"]) == 401
    assert (await login(client, 66, PASSWORD)).status_code == 401

    # Log in with the temporary password: allowed, but flagged.
    logged_in = (await login(client, 66, temporary)).json()
    assert logged_in["user"]["must_change_password"] is True
    access = logged_in["access_token"]

    # Gate: only /users/me, password change, logout and refresh are allowed.
    blocked = await client.patch("/api/v1/users/me", json={"name": "X"}, headers=bearer(access))
    assert blocked.status_code == 403 and error_code(blocked) == "PASSWORD_CHANGE_REQUIRED"
    for method, path in (
        ("GET", "/api/v1/users/me/addresses"),
        ("POST", "/api/v1/auth/logout-all"),
        ("GET", "/api/v1/admin/users"),
    ):
        response = await client.request(method, path, headers=bearer(access))
        assert error_code(response) == "PASSWORD_CHANGE_REQUIRED", path
    assert (await client.get("/api/v1/users/me", headers=bearer(access))).status_code == 200
    refreshed = await client.post("/api/v1/auth/refresh", json={"refresh_token": logged_in["refresh_token"]})
    assert refreshed.status_code == 200
    access = refreshed.json()["access_token"]

    changed = await change(client, access, temporary, NEW_PASSWORD)
    assert changed.status_code == 200, changed.text
    assert changed.json()["user"]["must_change_password"] is False
    new_access = changed.json()["access_token"]
    assert (await client.patch("/api/v1/users/me", json={"name": "Free"}, headers=bearer(new_access))).status_code == 200


async def test_logout_allowed_while_password_change_required(client: AsyncClient, db_session: AsyncSession) -> None:
    admin = await register(client, 67)
    await make_admin(db_session, admin["user"]["id"])
    customer = await register(client, 68)
    temporary = (
        await client.post(
            f"/api/v1/admin/users/{customer['user']['id']}/reset-password", headers=bearer(admin["access_token"])
        )
    ).json()["temporary_password"]
    session = (await login(client, 68, temporary)).json()

    response = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": session["refresh_token"]},
        headers=bearer(session["access_token"]),
    )
    assert response.status_code == 204
    assert await refresh_status(client, session["refresh_token"]) == 401


async def test_reset_password_of_google_account(client: AsyncClient, db_session: AsyncSession, google: dict) -> None:
    admin = await register(client, 69)
    await make_admin(db_session, admin["user"]["id"])
    google["g"] = GoogleIdentity(sub="g-70", email=None, name=None)
    signup = (await client.post("/api/v1/auth/google", json={"id_token": "g"})).json()["signup_token"]
    g_user = (
        await client.post(
            "/api/v1/auth/google/complete", json={"signup_token": signup, "name": "G", "phone": phone(70)}
        )
    ).json()["user"]

    response = await client.post(
        f"/api/v1/admin/users/{g_user['id']}/reset-password", headers=bearer(admin["access_token"])
    )
    assert response.status_code == 422
    assert error_code(response) == "NOT_A_PASSWORD_ACCOUNT"
