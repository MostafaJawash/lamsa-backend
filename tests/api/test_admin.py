from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.orders.models import Order
from conftest import FakeClock, bearer, error_code, make_admin, phone, register

pytestmark = pytest.mark.anyio

BASE = "/api/v1/admin/users"


async def admin_headers(client: AsyncClient, session: AsyncSession, n: int = 95) -> tuple[dict[str, str], int]:
    data = await register(client, n, name="Admin")
    await make_admin(session, data["user"]["id"])
    return bearer(data["access_token"]), data["user"]["id"]


async def test_non_admin_is_forbidden(client: AsyncClient) -> None:
    user = await register(client, 96)
    response = await client.get(BASE, headers=bearer(user["access_token"]))
    assert response.status_code == 403
    assert error_code(response) == "ADMIN_ONLY"


async def test_list_search_filter_paginate(
    client: AsyncClient, db_session: AsyncSession, frozen_clock: FakeClock
) -> None:
    headers, _ = await admin_headers(client, db_session)
    created = []
    for i, name in enumerate(["Mona Ali", "Omar Saleh", "Mona Hassan", "Zaid"]):
        frozen_clock.advance(seconds=10)
        created.append((await register(client, 10 + i, name=name))["user"])

    db_session.add(
        Order(
            user_id=created[0]["id"], customer_name="Mona Ali", customer_phone=phone(10),
            delivery_method_code="pickup", delivery_method_name="استلام",
            subtotal=Decimal("5"), shipping_fee=Decimal("0"), total=Decimal("5"),
        )
    )
    await db_session.commit()

    page = (await client.get(BASE, params={"page_size": 2}, headers=headers)).json()
    assert page["total"] == 5 and page["page"] == 1 and page["page_size"] == 2
    assert [u["name"] for u in page["items"]] == ["Zaid", "Mona Hassan"]  # newest first
    page2 = (await client.get(BASE, params={"page_size": 2, "page": 2}, headers=headers)).json()
    assert [u["name"] for u in page2["items"]] == ["Omar Saleh", "Mona Ali"]

    by_name = (await client.get(BASE, params={"search": "mona"}, headers=headers)).json()
    assert {u["name"] for u in by_name["items"]} == {"Mona Ali", "Mona Hassan"}
    mona = next(u for u in by_name["items"] if u["name"] == "Mona Ali")
    assert mona["orders_count"] == 1 and mona["is_active"] is True and "last_login_at" in mona

    by_phone = (await client.get(BASE, params={"search": "4567 12".replace(" ", "")}, headers=headers)).json()
    assert [u["name"] for u in by_phone["items"]] == ["Mona Hassan"]

    wildcard = (await client.get(BASE, params={"search": "%"}, headers=headers)).json()
    assert wildcard["total"] == 0  # % is matched literally

    admins = (await client.get(BASE, params={"role": "admin"}, headers=headers)).json()
    assert [u["name"] for u in admins["items"]] == ["Admin"]

    too_big = await client.get(BASE, params={"page_size": 101}, headers=headers)
    assert too_big.status_code == 422 and error_code(too_big) == "VALIDATION_ERROR"


async def test_get_and_update_user(client: AsyncClient, db_session: AsyncSession) -> None:
    headers, _ = await admin_headers(client, db_session)
    customer = await register(client, 20)
    user_id = customer["user"]["id"]

    detail = await client.get(f"{BASE}/{user_id}", headers=headers)
    assert detail.status_code == 200 and detail.json()["orders_count"] == 0
    missing = await client.get(f"{BASE}/999999999", headers=headers)
    assert missing.status_code == 404 and error_code(missing) == "USER_NOT_FOUND"

    deactivated = await client.patch(f"{BASE}/{user_id}", json={"is_active": False}, headers=headers)
    assert deactivated.status_code == 200 and deactivated.json()["is_active"] is False

    # Sessions are revoked and the access token stops working immediately.
    refreshed = await client.post("/api/v1/auth/refresh", json={"refresh_token": customer["refresh_token"]})
    assert refreshed.status_code == 401
    me = await client.get("/api/v1/users/me", headers=bearer(customer["access_token"]))
    assert me.status_code == 403 and error_code(me) == "ACCOUNT_DISABLED"

    promoted = await client.patch(f"{BASE}/{user_id}", json={"is_active": True, "role": "admin"}, headers=headers)
    assert promoted.json()["role"] == "admin" and promoted.json()["is_active"] is True


async def test_cannot_modify_self(client: AsyncClient, db_session: AsyncSession) -> None:
    headers, admin_id = await admin_headers(client, db_session)
    for body in ({"is_active": False}, {"role": "customer"}):
        response = await client.patch(f"{BASE}/{admin_id}", json=body, headers=headers)
        assert response.status_code == 422
        assert error_code(response) == "CANNOT_MODIFY_SELF"
    harmless = await client.patch(f"{BASE}/{admin_id}", json={"is_active": True, "role": "admin"}, headers=headers)
    assert harmless.status_code == 200
