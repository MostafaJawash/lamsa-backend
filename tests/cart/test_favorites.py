from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.favorites.models import Favorite
from cart_fixtures import ADMIN, CART, FAVORITES, build_shop, cart, set_setting
from catalog_helpers import code_of, ok
from conftest import bearer

pytestmark = pytest.mark.anyio

PUBLIC = "/api/v1"


@pytest.fixture
async def shop(client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession) -> dict[str, Any]:
    return await build_shop(client, admin_headers, db_session)


async def put(client: AsyncClient, headers: dict, product_id: int) -> Any:
    return await client.put(f"{FAVORITES}/{product_id}", headers=headers)


async def test_put_delete_idempotent_and_listing(client: AsyncClient, customer_headers: dict, shop: dict) -> None:
    for product in ("simple", "lip", "simple"):
        assert (await put(client, customer_headers, shop[product]["id"])).status_code == 204
    page = ok(await client.get(FAVORITES, headers=customer_headers))
    assert [i["name"] for i in page["items"]] == ["Lipstick", "Cream"] and page["total"] == 2  # newest first
    assert all(i["is_favorite"] for i in page["items"])
    assert page["items"][0]["swatches"] and page["items"][1]["compare_at_price"] == "10.00"  # real catalog cards
    assert ok(await client.get(f"{FAVORITES}/ids", headers=customer_headers)) == {"product_ids": [shop["lip"]["id"], shop["simple"]["id"]]}

    assert (await client.delete(f"{FAVORITES}/{shop['lip']['id']}", headers=customer_headers)).status_code == 204
    assert (await client.delete(f"{FAVORITES}/{shop['lip']['id']}", headers=customer_headers)).status_code == 204
    assert (await client.delete(f"{FAVORITES}/999999", headers=customer_headers)).status_code == 204
    assert ok(await client.get(f"{FAVORITES}/ids", headers=customer_headers))["product_ids"] == [shop["simple"]["id"]]
    missing = await put(client, customer_headers, 999_999)
    assert missing.status_code == 404 and code_of(missing) == "PRODUCT_NOT_FOUND"


async def test_invisible_favorite_hidden_but_kept(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict, db_session: AsyncSession
) -> None:
    assert (await put(client, customer_headers, shop["simple"]["id"])).status_code == 204
    ok(await client.patch(f"{ADMIN}/products/{shop['simple']['id']}", json={"is_active": False}, headers=admin_headers))
    page = ok(await client.get(FAVORITES, headers=customer_headers))
    assert page == {"items": [], "page": 1, "page_size": 20, "total": 0}
    assert ok(await client.get(f"{FAVORITES}/ids", headers=customer_headers))["product_ids"] == []
    rows = await db_session.scalar(select(func.count()).select_from(Favorite).where(Favorite.product_id == shop["simple"]["id"]))
    assert rows == 1
    hidden = await put(client, customer_headers, shop["simple"]["id"])
    assert hidden.status_code == 404 and code_of(hidden) == "PRODUCT_NOT_FOUND"

    ok(await client.patch(f"{ADMIN}/products/{shop['simple']['id']}", json={"is_active": True}, headers=admin_headers))
    assert ok(await client.get(FAVORITES, headers=customer_headers))["total"] == 1  # back after re-publish


async def test_favorites_limit_and_merge(client: AsyncClient, customer_headers: dict, shop: dict, db_session: AsyncSession) -> None:
    await set_setting(db_session, "favorites_max", 2)
    assert (await put(client, customer_headers, shop["simple"]["id"])).status_code == 204
    assert (await put(client, customer_headers, shop["lip"]["id"])).status_code == 204
    full = await put(client, customer_headers, shop["perfume"]["id"])
    assert full.status_code == 422 and code_of(full) == "FAVORITES_LIMIT_REACHED" and full.json()["error"]["details"] == {"max": 2}
    assert (await put(client, customer_headers, shop["lip"]["id"])).status_code == 204  # already there: fine

    await set_setting(db_session, "favorites_max", 3)
    merged = ok(await client.post(f"{FAVORITES}/merge", json={"product_ids": [
        shop["lip"]["id"], shop["perfume"]["id"], shop["bundle"]["id"], 999_999, shop["perfume"]["id"],
    ]}, headers=customer_headers))
    assert merged["added"] == [shop["lip"]["id"], shop["perfume"]["id"]]
    assert merged["skipped"] == [
        {"product_id": shop["bundle"]["id"], "reason": "FAVORITES_LIMIT_REACHED"},
        {"product_id": 999_999, "reason": "PRODUCT_NOT_FOUND"},
    ]
    too_many = await client.post(f"{FAVORITES}/merge", json={"product_ids": list(range(201))}, headers=customer_headers)
    assert too_many.status_code == 422


async def test_move_to_cart(client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict) -> None:
    for product in ("simple", "lip", "perfume"):
        await put(client, customer_headers, shop[product]["id"])

    given = ok(await client.post(f"{FAVORITES}/{shop['simple']['id']}/move-to-cart", json={"quantity": 2}, headers=customer_headers))
    assert given["lines"][0]["quantity"] == 2
    picked = ok(await client.post(f"{FAVORITES}/{shop['lip']['id']}/move-to-cart", json={}, headers=customer_headers))
    lip_line = next(l for l in picked["lines"] if l["product"]["id"] == shop["lip"]["id"])
    assert lip_line["variant"]["id"] == shop["red"]["id"]  # first active variant in stock by sort_order
    assert ok(await client.get(f"{FAVORITES}/ids", headers=customer_headers))["product_ids"] == [shop["perfume"]["id"]]

    for v in ("p50", "p100"):
        ok(await client.post(f"{ADMIN}/products/{shop['perfume']['id']}/variants/{shop[v]['id']}/stock-adjust", json={"delta": -4}, headers=admin_headers))
    none_in_stock = await client.post(f"{FAVORITES}/{shop['perfume']['id']}/move-to-cart", json={}, headers=customer_headers)
    assert none_in_stock.status_code == 409 and code_of(none_in_stock) == "OUT_OF_STOCK"
    assert ok(await client.get(f"{FAVORITES}/ids", headers=customer_headers))["product_ids"] == [shop["perfume"]["id"]]  # kept
    assert len((await cart(client, customer_headers))["lines"]) == 2


async def test_is_favorite_in_catalog(client: AsyncClient, customer_headers: dict, shop: dict) -> None:
    await put(client, customer_headers, shop["lip"]["id"])
    logged_in = {i["id"]: i["is_favorite"] for i in ok(await client.get(f"{PUBLIC}/products", headers=customer_headers))["items"]}
    assert logged_in[shop["lip"]["id"]] is True and logged_in[shop["simple"]["id"]] is False
    anonymous = ok(await client.get(f"{PUBLIC}/products"))["items"]
    assert all(i["is_favorite"] is False for i in anonymous)

    assert ok(await client.get(f"{PUBLIC}/products/{shop['lip']['slug']}", headers=customer_headers))["is_favorite"] is True
    assert ok(await client.get(f"{PUBLIC}/products/{shop['lip']['slug']}"))["is_favorite"] is False
    related = ok(await client.get(f"{PUBLIC}/products/{shop['simple']['slug']}/related", headers=customer_headers))
    assert {r["id"]: r["is_favorite"] for r in related}[shop["lip"]["id"]] is True

    for headers in (bearer("garbage"), {"Authorization": "Basic abc"}):
        response = await client.get(f"{PUBLIC}/products", headers=headers)
        assert response.status_code == 401, headers


async def test_optional_user_skips_password_change_gate(
    client: AsyncClient, admin_headers: dict, shop: dict, db_session: AsyncSession
) -> None:
    from conftest import login, register

    customer = await register(client, 66)
    temporary = ok(await client.post(f"{ADMIN}/users/{customer['user']['id']}/reset-password", headers=admin_headers))["temporary_password"]
    session = (await login(client, 66, temporary)).json()
    headers = bearer(session["access_token"])
    assert (await client.get(f"{PUBLIC}/products", headers=headers)).status_code == 200  # browsing still works
    gated = await client.get(CART, headers=headers)
    assert gated.status_code == 403 and code_of(gated) == "PASSWORD_CHANGE_REQUIRED"
