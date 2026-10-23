import re
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.modules.products.models import Product
from app.shared import events
from cart_fixtures import add, build_shop
from catalog_helpers import code_of, ok
from order_helpers import ADMIN_ORDERS, ORDERS, address, order_from_cart, place

pytestmark = pytest.mark.anyio


@pytest.fixture
async def shop(client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession) -> dict[str, Any]:
    return await build_shop(client, admin_headers, db_session)


@pytest.fixture
def recorded() -> Iterator[list[events.DomainEvent]]:
    seen: list[events.DomainEvent] = []

    async def record(event: events.DomainEvent) -> None:
        seen.append(event)

    events.subscribe(events.DomainEvent, record)
    yield seen
    events.unsubscribe(record)


# ------------------------------------------------------------------------------- events


async def test_events_after_commit(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict, recorded: list
) -> None:
    order = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 3)], method="pickup")
    # Cream 5 -> 2 crosses the threshold (3): StockLow.
    assert [type(e).__name__ for e in recorded] == ["OrderPlaced", "StockLow"]
    placed, low = recorded
    assert (placed.order_id, placed.total) == (order["id"], "24.00")
    assert (low.product_id, low.variant_id, low.stock, low.threshold) == (shop["simple"]["id"], None, 2, 3)

    recorded.clear()
    ok(await client.post(f"{ADMIN_ORDERS}/{order['id']}/status", json={"to_status": "preparing"}, headers=admin_headers))
    assert [(type(e).__name__, e.from_status, e.to_status) for e in recorded] == [("OrderStatusChanged", "pending", "preparing")]
    recorded.clear()
    ok(await client.post(f"{ADMIN_ORDERS}/{order['id']}/cancel", json={"reason": "out of reach"}, headers=admin_headers))
    assert [(type(e).__name__, e.by, e.stock_restored) for e in recorded] == [("OrderCancelled", "admin", True)]


async def test_no_events_when_the_transaction_fails(
    client: AsyncClient, customer_headers: dict, shop: dict, recorded: list, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok(await add(client, customer_headers, shop["simple"]["id"]), 201)
    rejected = await place(client, customer_headers, method="pickup", total="1.00")  # PRICE_CHANGED
    assert code_of(rejected) == "PRICE_CHANGED" and recorded == []

    async def failing_commit() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db_session, "commit", failing_commit)
    failed = await place(client, customer_headers, method="pickup")
    assert failed.status_code == 500 and recorded == []
    monkeypatch.undo()
    stock = (await client.get(f"/api/v1/admin/products/{shop['simple']['id']}", headers=await _admin(client, db_session))).json()["stock"]
    assert stock == 5  # rolled back


async def _admin(client: AsyncClient, session: AsyncSession) -> dict[str, str]:
    from conftest import bearer, make_admin, register

    data = await register(client, 60, name="Admin 2")
    await make_admin(session, data["user"]["id"])
    return bearer(data["access_token"])


async def test_failing_handler_does_not_break_the_request(
    client: AsyncClient, customer_headers: dict, shop: dict
) -> None:
    async def broken(event: events.DomainEvent) -> None:
        raise RuntimeError("push service down")

    events.subscribe(events.OrderPlaced, broken)
    try:
        order = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 1)], method="pickup")
    finally:
        events.unsubscribe(broken)
    assert ok(await client.get(f"{ORDERS}/{order['id']}", headers=customer_headers))["status"] == "pending"


# --------------------------------------------------------------------------------- auth

ROUTES = sorted(
    (method.upper(), path)
    for path, operations in app.openapi()["paths"].items()
    if "/orders" in path
    for method in operations
)


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


@pytest.mark.parametrize(("method", "path"), [r for r in ROUTES if "/admin/" not in r[1]])
async def test_customer_routes_need_login(client: AsyncClient, method: str, path: str) -> None:
    assert (await client.request(method, concrete(path))).status_code == 401


@pytest.mark.parametrize(("method", "path"), [r for r in ROUTES if "/admin/" in r[1]])
async def test_admin_routes_refuse_customers(client: AsyncClient, customer_headers: dict, method: str, path: str) -> None:
    response = await client.request(method, concrete(path), headers=customer_headers)
    assert response.status_code == 403 and response.json()["error"]["code"] == "ADMIN_ONLY"


# ------------------------------------------------------------------------------- budget


async def test_read_budgets(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict, db_session: AsyncSession, count_queries: Any
) -> None:
    await db_session.execute(update(Product).where(Product.id == shop["simple"]["id"]).values(stock=50))
    await db_session.commit()
    addr = await address(client, customer_headers)
    order_ids = []
    for _ in range(3):
        order = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 1), (shop["bundle"]["id"], None, 1),
                                                                 (shop["lip"]["id"], shop["pink"]["id"], 1) if not order_ids else (shop["perfume"]["id"], shop["p50"]["id"], 1)],
                                      address_id=addr["id"])
        order_ids.append(order["id"])
    ok(await client.post(f"{ADMIN_ORDERS}/{order_ids[0]}/status", json={"to_status": "preparing"}, headers=admin_headers))

    measured: dict[str, tuple[int, int]] = {}
    for name, url, headers, limit in (
        ("GET /orders", ORDERS, customer_headers, 4),
        ("GET /orders/{id}", f"{ORDERS}/{order_ids[0]}", customer_headers, 5),
        ("GET /admin/orders", ADMIN_ORDERS, admin_headers, 4),
        ("GET /admin/orders/{id}", f"{ADMIN_ORDERS}/{order_ids[0]}", admin_headers, 6),
        ("GET /admin/orders/stats", f"{ADMIN_ORDERS}/stats", admin_headers, 6),
    ):
        with count_queries() as counter:
            ok(await client.get(url, headers=headers))
        measured[name] = (counter.count, limit)
    import os

    if os.environ.get("ORDER_STATEMENTS_FILE"):
        with open(os.environ["ORDER_STATEMENTS_FILE"], "w", encoding="utf-8") as out:
            out.writelines(f"{name}: {count} / {limit}\n" for name, (count, limit) in measured.items())
    for name, (count, limit) in measured.items():
        assert count <= limit, (name, count)
