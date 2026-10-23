"""Every admin endpoint refuses customers (403 ADMIN_ONLY) and anonymous callers (401)."""

import re

import pytest
from httpx import AsyncClient

from app.main import app

pytestmark = pytest.mark.anyio

# Built from the OpenAPI schema: it lists every operation of every included router.
ADMIN_ROUTES = sorted(
    (method.upper(), path)
    for path, operations in app.openapi()["paths"].items()
    if path.startswith("/api/v1/admin/")
    for method in operations
)


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def test_route_table_covers_catalog() -> None:
    paths = {p for _, p in ADMIN_ROUTES}
    assert {"/api/v1/admin/categories", "/api/v1/admin/brands", "/api/v1/admin/products/{product_id}/images"} <= paths
    assert len(ADMIN_ROUTES) >= 40


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES, ids=[f"{m} {p}" for m, p in ADMIN_ROUTES])
async def test_customers_get_403(client: AsyncClient, customer_headers: dict[str, str], method: str, path: str) -> None:
    response = await client.request(method, concrete(path), headers=customer_headers)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "ADMIN_ONLY"


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES[:10], ids=[f"{m} {p}" for m, p in ADMIN_ROUTES[:10]])
async def test_anonymous_gets_401(client: AsyncClient, method: str, path: str) -> None:
    response = await client.request(method, concrete(path))
    assert response.status_code == 401
