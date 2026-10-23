from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock
from app.modules.orders.models import Order
from app.modules.products.models import Product
from cart_fixtures import ADMIN, build_shop
from catalog_helpers import code_of, ok
from conftest import bearer, register
from order_helpers import ADMIN_ORDERS, ORDERS, order_from_cart

pytestmark = pytest.mark.anyio


@pytest.fixture
async def shop(client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession) -> dict[str, Any]:
    shop = await build_shop(client, admin_headers, db_session)
    await db_session.execute(update(Product).where(Product.id == shop["simple"]["id"]).values(stock=100))
    await db_session.commit()
    return shop


async def move(client: AsyncClient, admin: dict, order_id: int, to: str) -> Any:
    return await client.post(f"{ADMIN_ORDERS}/{order_id}/status", json={"to_status": to, "note": "ok"}, headers=admin)


async def test_delivery_workflow(client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict) -> None:
    order = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 1)])
    detail = ok(await client.get(f"{ADMIN_ORDERS}/{order['id']}", headers=admin_headers))
    assert detail["allowed_next_statuses"] == ["preparing"] and detail["can_cancel"] is True

    for wrong in ("shipped", "delivered", "ready_for_pickup", "pending", "cancelled"):
        response = await move(client, admin_headers, order["id"], wrong)
        assert response.status_code == 422 and code_of(response) == "INVALID_STATUS_TRANSITION", wrong
        assert response.json()["error"]["details"] == {"from": "pending", "to": wrong, "allowed": ["preparing"]}

    expected_next = {"preparing": ["shipped"], "shipped": ["delivered"], "delivered": []}
    for step in ("preparing", "shipped", "delivered"):
        detail = ok(await move(client, admin_headers, order["id"], step))
        assert detail["status"] == step and detail["allowed_next_statuses"] == expected_next[step]
    assert (detail["payment_status"], detail["status_label"], detail["can_cancel"]) == ("paid", "وصل", False)
    assert [(h["from_status"], h["to_status"]) for h in detail["history"]] == [
        (None, "pending"), ("pending", "preparing"), ("preparing", "shipped"), ("shipped", "delivered")]
    admin_name = (await client.get("/api/v1/users/me", headers=admin_headers)).json()["name"]
    assert detail["history"][1]["changed_by_name"] == admin_name and detail["history"][1]["note"] == "ok"
    assert detail["history"][0]["changed_by_name"] == "Customer"
    assert detail["customer"]["orders_count"] == 1
    finished = await client.post(f"{ADMIN_ORDERS}/{order['id']}/cancel", json={"reason": "late"}, headers=admin_headers)
    assert finished.status_code == 409 and code_of(finished) == "ORDER_NOT_CANCELLABLE"


async def test_pickup_workflow(client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict) -> None:
    order = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 1)], method="pickup")
    ok(await move(client, admin_headers, order["id"], "preparing"))
    shipped = await move(client, admin_headers, order["id"], "shipped")
    assert code_of(shipped) == "INVALID_STATUS_TRANSITION" and shipped.json()["error"]["details"]["allowed"] == ["ready_for_pickup"]
    ready = ok(await move(client, admin_headers, order["id"], "ready_for_pickup"))
    assert ready["status_label"] == "جاهز للاستلام"
    done = ok(await move(client, admin_headers, order["id"], "delivered"))
    assert (done["status_label"], done["payment_status"]) == ("تم الاستلام", "paid")

    delivery = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 1)])
    ok(await move(client, admin_headers, delivery["id"], "preparing"))
    wrong = await move(client, admin_headers, delivery["id"], "ready_for_pickup")
    assert code_of(wrong) == "INVALID_STATUS_TRANSITION"


@pytest.mark.parametrize("steps", [[], ["preparing"], ["preparing", "shipped"]])
async def test_admin_cancel_from_each_status(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict, db_session: AsyncSession, steps: list[str]
) -> None:
    order = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 3)])
    for step in steps:
        ok(await move(client, admin_headers, order["id"], step))
    short = await client.post(f"{ADMIN_ORDERS}/{order['id']}/cancel", json={"reason": "x"}, headers=admin_headers)
    assert short.status_code == 422
    cancelled = ok(await client.post(f"{ADMIN_ORDERS}/{order['id']}/cancel", json={"reason": "رفض الاستلام"}, headers=admin_headers))
    assert cancelled["status"] == "cancelled" and cancelled["history"][-1]["note"] == "رفض الاستلام"
    stock = await db_session.scalar(select(Product.stock).where(Product.id == shop["simple"]["id"]).execution_options(populate_existing=True))
    assert stock == 100


async def test_ready_for_pickup_cancel_and_restore_stock_rules(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict, db_session: AsyncSession
) -> None:
    pickup = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 2)], method="pickup")
    for step in ("preparing", "ready_for_pickup"):
        ok(await move(client, admin_headers, pickup["id"], step))
    keep = await client.post(f"{ADMIN_ORDERS}/{pickup['id']}/cancel", json={"reason": "never picked up", "restore_stock": False}, headers=admin_headers)
    assert keep.status_code == 422 and code_of(keep) == "VALIDATION_ERROR"
    ok(await client.post(f"{ADMIN_ORDERS}/{pickup['id']}/cancel", json={"reason": "never picked up"}, headers=admin_headers))

    shipped = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 4)])
    for step in ("preparing", "shipped"):
        ok(await move(client, admin_headers, shipped["id"], step))
    ok(await client.post(f"{ADMIN_ORDERS}/{shipped['id']}/cancel", json={"reason": "lost in transit", "restore_stock": False}, headers=admin_headers))
    stock = await db_session.scalar(select(Product.stock).where(Product.id == shop["simple"]["id"]).execution_options(populate_existing=True))
    assert stock == 96  # the lost 4 were not put back


async def test_admin_list_filters_and_sorts(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict, db_session: AsyncSession
) -> None:
    a = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 1)], method="pickup")
    other = bearer((await register(client, 72, name="Mona Haddad"))["access_token"])
    b = await order_from_cart(client, other, [(shop["simple"]["id"], None, 3)])
    ok(await move(client, admin_headers, b["id"], "preparing"))
    await db_session.execute(update(Order).where(Order.id == a["id"]).values(created_at=clock.utcnow() - timedelta(days=3)))
    await db_session.commit()

    def ids(page: dict) -> list[int]:
        return [o["id"] for o in page["items"]]

    async def listing(**params: Any) -> dict:
        data: dict = ok(await client.get(ADMIN_ORDERS, params=params, headers=admin_headers))
        return data

    assert ids(await listing()) == [b["id"], a["id"]]
    assert ids(await listing(sort="oldest")) == [a["id"], b["id"]]
    assert ids(await listing(sort="total_desc")) == [b["id"], a["id"]]
    assert ids(await listing(status="preparing")) == [b["id"]]
    assert ids(await listing(delivery_method="pickup")) == [a["id"]]
    assert ids(await listing(q=str(a["id"]))) == [a["id"]]
    assert ids(await listing(q="mona")) == [b["id"]]
    phone = (await client.get("/api/v1/users/me", headers=other)).json()["phone"]
    assert ids(await listing(q=phone)) == [b["id"]]
    today = clock.utcnow().date()
    assert ids(await listing(date_from=str(today), date_to=str(today))) == [b["id"]]  # date_to inclusive
    assert ids(await listing(date_to=str(today - timedelta(days=1)))) == [a["id"]]
    row = (await listing(q="mona"))["items"][0]
    assert (row["customer_name"], row["customer_phone"], row["items_count"]) == ("Mona Haddad", phone, 3)


async def test_stats(client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict) -> None:
    o1 = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 2)], method="pickup")  # 16.00
    o2 = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 1), (shop["lip"]["id"], shop["red"]["id"], 1)], method="pickup")  # 20.00
    o3 = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 1)], method="pickup")  # 8.00
    for order in (o1, o2):
        for step in ("preparing", "ready_for_pickup", "delivered"):
            ok(await move(client, admin_headers, order["id"], step))
    ok(await client.post(f"{ADMIN_ORDERS}/{o3['id']}/cancel", json={"reason": "duplicate"}, headers=admin_headers))
    await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 1)], method="pickup")  # pending

    stats = ok(await client.get(f"{ADMIN_ORDERS}/stats", headers=admin_headers))
    assert stats["orders_count"] == 4
    assert stats["counts"] == {"pending": 1, "preparing": 0, "shipped": 0, "ready_for_pickup": 0, "delivered": 2, "cancelled": 1}
    assert (stats["delivered_revenue"], stats["average_order_value"], stats["cancelled_rate"]) == ("36.00", "18.00", 0.25)
    assert stats["top_products"] == [
        {"product_id": shop["simple"]["id"], "name": "Cream", "quantity": 3},
        {"product_id": shop["lip"]["id"], "name": "Lipstick", "quantity": 1},
    ]
    tomorrow = str(clock.utcnow().date() + timedelta(days=1))
    empty = ok(await client.get(f"{ADMIN_ORDERS}/stats", params={"date_from": tomorrow}, headers=admin_headers))
    assert (empty["orders_count"], empty["delivered_revenue"], empty["cancelled_rate"]) == (0, "0.00", 0.0)
    assert ok(await client.get(f"{ORDERS}/{o1['id']}", headers=customer_headers))["status"] == "delivered"


# ------------------------------------------------------------------ numeric search


@pytest.fixture
async def two_orders(client: AsyncClient, customer_headers: dict, shop: dict, db_session: AsyncSession) -> dict[str, Any]:
    first = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 1)], method="pickup")
    other = bearer((await register(client, 73, name="Rana"))["access_token"])
    second = await order_from_cart(client, other, [(shop["simple"]["id"], None, 1)], method="pickup")
    phone = "+4915123456773"  # register(73)
    return {"first": first, "second": second, "phone": phone}


async def search(client: AsyncClient, admin: dict, q: str, **params: Any) -> list[int]:
    page = ok(await client.get(ADMIN_ORDERS, params={"q": q, **params}, headers=admin))
    return [o["id"] for o in page["items"]]


async def test_numeric_q_exact_order_number(client: AsyncClient, admin_headers: dict, two_orders: dict) -> None:
    assert await search(client, admin_headers, str(two_orders["first"]["id"])) == [two_orders["first"]["id"]]


@pytest.mark.parametrize("q", ["6773", "0151 23456773", "015123456773", "+49 151 23456773", "+4915123456773", "0049-151-234-567-73"])
async def test_numeric_q_matches_phone_formats(client: AsyncClient, admin_headers: dict, two_orders: dict, q: str) -> None:
    assert await search(client, admin_headers, q) == [two_orders["second"]["id"]], q


async def test_numeric_q_matching_both_puts_exact_order_first(
    client: AsyncClient, admin_headers: dict, two_orders: dict, db_session: AsyncSession
) -> None:
    first_id = two_orders["first"]["id"]
    # Make the second order's phone snapshot contain the first order's number.
    await db_session.execute(update(Order).where(Order.id == two_orders["second"]["id"]).values(customer_phone=f"+49151{first_id}99"))
    await db_session.commit()
    for sort in ("newest", "oldest", "total_desc"):
        assert await search(client, admin_headers, str(first_id), sort=sort) == [first_id, two_orders["second"]["id"]], sort


async def test_numeric_q_no_match(client: AsyncClient, admin_headers: dict, two_orders: dict) -> None:
    assert await search(client, admin_headers, "987654") == []
    assert await search(client, admin_headers, "000") == []
