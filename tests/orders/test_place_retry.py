"""A deadlock during placement is retried once and the order is placed (found by the load test:
the retry used to read the user's name from an ORM object expired by the rollback -> 500)."""

from typing import Any

import pytest
from httpx import AsyncClient
from pymysql.err import OperationalError as MySQLOperationalError
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.orders import stock
from cart_fixtures import build_shop
from catalog_helpers import ok
from order_helpers import order_from_cart

pytestmark = pytest.mark.anyio


@pytest.fixture
async def shop(client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession) -> dict[str, Any]:
    return await build_shop(client, admin_headers, db_session)


async def test_deadlock_after_the_user_was_loaded_is_retried(
    client: AsyncClient, customer_headers: dict, shop: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_deduct = stock.deduct
    calls = 0

    async def deadlock_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("UPDATE products ...", {}, MySQLOperationalError(1213, "Deadlock found"))
        return await real_deduct(*args, **kwargs)

    monkeypatch.setattr(stock, "deduct", deadlock_once)
    order = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 1)], method="pickup")
    assert calls == 2 and order["status"] == "pending"  # the second attempt placed it
    assert ok(await client.get(f"/api/v1/orders/{order['id']}", headers=customer_headers))["id"] == order["id"]


def _deadlocking(real: Any, failures: int) -> tuple[Any, list[int]]:
    calls: list[int] = []

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) <= failures:
            raise OperationalError("INSERT ...", {}, MySQLOperationalError(1213, "Deadlock found"))
        return await real(*args, **kwargs)

    return wrapper, calls


async def test_two_deadlocks_still_succeed_and_a_persistent_one_is_service_busy(
    client: AsyncClient, customer_headers: dict, shop: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.cart import repository as cart_repo

    wrapper, calls = _deadlocking(cart_repo.insert_lines, failures=2)
    monkeypatch.setattr(cart_repo, "insert_lines", wrapper)
    added = await client.post("/api/v1/cart/items", json={"product_id": shop["simple"]["id"], "quantity": 1}, headers=customer_headers)
    assert added.status_code == 201 and len(calls) == 3

    wrapper, calls = _deadlocking(stock.deduct, failures=99)
    monkeypatch.setattr(stock, "deduct", wrapper)
    from order_helpers import place

    response = await place(client, customer_headers, method="pickup")
    assert response.status_code == 503 and response.json()["error"]["code"] == "SERVICE_BUSY"
    assert response.headers["retry-after"] == "1" and len(calls) == 3  # three attempts, then shed load
    monkeypatch.undo()
    assert (await place(client, customer_headers, method="pickup")).status_code == 201  # nothing half-done
