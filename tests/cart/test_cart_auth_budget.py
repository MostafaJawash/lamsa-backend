import re
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from cart_fixtures import ADMIN, CART, FAVORITES, add, build_shop
from catalog_helpers import ok
from conftest import bearer, login, register

pytestmark = pytest.mark.anyio

ROUTES = sorted(
    (method.upper(), path)
    for path, operations in app.openapi()["paths"].items()
    if path.startswith(("/api/v1/cart", "/api/v1/favorites"))
    for method in operations
)


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def test_route_table() -> None:
    assert len(ROUTES) == 13


@pytest.mark.parametrize(("method", "path"), ROUTES, ids=[f"{m} {p}" for m, p in ROUTES])
async def test_requires_login(client: AsyncClient, method: str, path: str) -> None:
    response = await client.request(method, concrete(path))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


@pytest.fixture
async def flagged_headers(client: AsyncClient, admin_headers: dict[str, str]) -> dict[str, str]:
    customer = await register(client, 55)
    temporary = ok(await client.post(f"{ADMIN}/users/{customer['user']['id']}/reset-password", headers=admin_headers))["temporary_password"]
    return bearer((await login(client, 55, temporary)).json()["access_token"])


@pytest.mark.parametrize(("method", "path"), ROUTES, ids=[f"{m} {p}" for m, p in ROUTES])
async def test_password_change_gate(client: AsyncClient, flagged_headers: dict[str, str], method: str, path: str) -> None:
    response = await client.request(method, concrete(path), headers=flagged_headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"


async def test_query_budgets(
    client: AsyncClient, admin_headers: dict[str, str], customer_headers: dict[str, str], db_session: AsyncSession, count_queries: Any
) -> None:
    shop = await build_shop(client, admin_headers, db_session)
    with count_queries() as empty_cart:
        ok(await client.get(CART, headers=customer_headers))
    with count_queries() as first_add:
        ok(await add(client, customer_headers, shop["bundle"]["id"]), 201)
    ok(await add(client, customer_headers, shop["lip"]["id"], shop["red"]["id"]), 201)
    with count_queries() as mixed_add:
        ok(await add(client, customer_headers, shop["perfume"]["id"], shop["p50"]["id"]), 201)
    with count_queries() as read:
        data = ok(await client.get(CART, headers=customer_headers))
    item_id = data["lines"][0]["id"]

    budgets: dict[str, Any] = {"empty GET": (empty_cart, 6), "first add": (first_add, 10),
                               "add to mixed cart": (mixed_add, 10), "GET mixed cart": (read, 6)}
    mutations = [
        ("PATCH quantity", "PATCH", f"{CART}/items/{item_id}", {"quantity": 2}),
        ("PATCH variant", "PATCH", f"{CART}/items/{data['lines'][1]['id']}", {"variant_id": shop["pink"]["id"]}),
        ("DELETE item", "DELETE", f"{CART}/items/{item_id}", None),
        ("resolve", "POST", f"{CART}/resolve-issues", None),
        ("merge", "POST", f"{CART}/merge", {"items": [{"product_id": shop["simple"]["id"]}, {"product_id": shop["bundle"]["id"]},
                                                       {"product_id": shop["lip"]["id"], "variant_id": shop["red"]["id"]},
                                                       {"product_id": shop["perfume"]["id"], "variant_id": shop["p100"]["id"]}]}),
    ]
    for name, method, url, body in mutations:
        with count_queries() as counter:
            ok(await client.request(method, url, json=body, headers=customer_headers))
        budgets[name] = (counter, 10)

    await client.put(f"{FAVORITES}/{shop['simple']['id']}", headers=customer_headers)
    await client.put(f"{FAVORITES}/{shop['bundle']['id']}", headers=customer_headers)
    with count_queries() as move:
        ok(await client.post(f"{FAVORITES}/{shop['bundle']['id']}/move-to-cart", json={}, headers=customer_headers))
    budgets["move-to-cart (bundle, mixed cart)"] = (move, 10)
    with count_queries() as favorites:
        ok(await client.get(FAVORITES, headers=customer_headers))
    budgets["GET favorites"] = (favorites, 6)
    with count_queries() as clear:
        ok(await client.delete(CART, headers=customer_headers))
    budgets["DELETE cart"] = (clear, 10)

    for name, (counter, limit) in budgets.items():
        print(f"budget {name:36} {counter.count:2} / {limit}")
        assert counter.count <= limit, (name, counter.count, counter.statements)

    # Catalog: anonymous unchanged, logged-in at most +1 (the favorites lookup).
    with count_queries() as anonymous:
        ok(await client.get("/api/v1/products"))
    with count_queries() as personal:
        ok(await client.get("/api/v1/products", headers=customer_headers))
    with count_queries() as detail_anon:
        ok(await client.get(f"/api/v1/products/{shop['lip']['slug']}"))
    with count_queries() as detail_user:
        ok(await client.get(f"/api/v1/products/{shop['lip']['slug']}", headers=customer_headers))
    assert anonymous.count <= 6 and detail_anon.count <= 6
    # The user's own row is read by authentication; the favorites lookup is the only extra query.
    assert personal.count - anonymous.count <= 2 and detail_user.count - detail_anon.count <= 2
    assert personal.count <= 7 + 1 and detail_user.count <= 7 + 1
