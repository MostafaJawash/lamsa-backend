from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.orders.models import OrderItem, OrderStatusHistory
from app.modules.products.models import Product, ProductVariant
from cart_fixtures import ADMIN, build_shop, cart
from catalog_helpers import code_of, ok
from conftest import bearer, register
from order_helpers import ADMIN_ORDERS, ORDERS, order_from_cart

pytestmark = pytest.mark.anyio


@pytest.fixture
async def shop(client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession) -> dict[str, Any]:
    return await build_shop(client, admin_headers, db_session)


async def stocks(session: AsyncSession, shop: dict) -> tuple[int, int, int]:
    simple = await session.scalar(select(Product.stock).where(Product.id == shop["simple"]["id"]).execution_options(populate_existing=True))
    red = await session.scalar(select(ProductVariant.stock).where(ProductVariant.id == shop["red"]["id"]).execution_options(populate_existing=True))
    p50 = await session.scalar(select(ProductVariant.stock).where(ProductVariant.id == shop["p50"]["id"]).execution_options(populate_existing=True))
    return int(simple or 0), int(red or 0), int(p50 or 0)


async def test_list_detail_and_foreign_order(client: AsyncClient, customer_headers: dict, shop: dict) -> None:
    first = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 1)], method="pickup")
    second = await order_from_cart(client, customer_headers, [(shop["lip"]["id"], shop["red"]["id"], 2)], method="pickup")
    page = ok(await client.get(ORDERS, headers=customer_headers))
    assert [o["id"] for o in page["items"]] == [second["id"], first["id"]] and page["total"] == 2
    row = page["items"][0]
    assert (row["status"], row["status_label"], row["items_count"], row["total"], row["delivery_method_name"]) == (
        "pending", "تم استلام طلبك", 2, "24.00", "الاستلام من المتجر")
    assert row["image"] == shop["color_images"]["lip_red"]["urls"]
    paged = ok(await client.get(ORDERS, params={"page_size": 1, "page": 2}, headers=customer_headers))
    assert [o["id"] for o in paged["items"]] == [first["id"]] and paged["total"] == 2
    assert ok(await client.get(ORDERS, params={"status": "cancelled"}, headers=customer_headers))["total"] == 0

    detail = ok(await client.get(f"{ORDERS}/{first['id']}", headers=customer_headers))
    assert detail["id"] == first["id"] and detail["items"][0]["name"] == "Cream" and detail["can_cancel"] is True
    other = bearer((await register(client, 71))["access_token"])
    foreign = await client.get(f"{ORDERS}/{first['id']}", headers=other)
    assert foreign.status_code == 404 and code_of(foreign) == "ORDER_NOT_FOUND"
    assert (await client.post(f"{ORDERS}/{first['id']}/cancel", headers=other)).status_code == 404


async def test_cancel_restores_stock_including_bundles(
    client: AsyncClient, customer_headers: dict, shop: dict, db_session: AsyncSession, admin_headers: dict
) -> None:
    before = await stocks(db_session, shop)
    order = await order_from_cart(client, customer_headers, [
        (shop["simple"]["id"], None, 1), (shop["bundle"]["id"], None, 1), (shop["perfume"]["id"], shop["p50"]["id"], 2),
    ], method="pickup")
    assert await stocks(db_session, shop) == (before[0] - 3, before[1] - 1, before[2] - 2)

    cancelled = ok(await client.post(f"{ORDERS}/{order['id']}/cancel", json={"reason": "  غيرت رأيي  "}, headers=customer_headers))
    assert (cancelled["status"], cancelled["status_label"], cancelled["can_cancel"], cancelled["cancel_reason"]) == ("cancelled", "ملغى", False, "غيرت رأيي")
    assert [(s["status"], s["is_current"]) for s in cancelled["timeline"]] == [("pending", False), ("cancelled", True)]
    assert cancelled["timeline"][-1]["cancel_reason"] == "غيرت رأيي"
    assert await stocks(db_session, shop) == before
    box = ok(await client.get(f"{ADMIN}/products/{shop['bundle']['id']}", headers=admin_headers))
    assert box["in_stock"] is True  # summaries recalculated

    history = (await db_session.scalars(select(OrderStatusHistory).where(OrderStatusHistory.order_id == order["id"]).order_by(OrderStatusHistory.id))).all()
    assert [(h.from_status and h.from_status.value, h.to_status.value) for h in history] == [(None, "pending"), ("pending", "cancelled")]

    again = await client.post(f"{ORDERS}/{order['id']}/cancel", headers=customer_headers)
    assert again.status_code == 409 and code_of(again) == "ORDER_NOT_CANCELLABLE" and again.json()["error"]["details"] == {"status": "cancelled"}
    assert await stocks(db_session, shop) == before  # never restored twice


async def test_customer_cannot_cancel_after_pending(
    client: AsyncClient, customer_headers: dict, admin_headers: dict, shop: dict
) -> None:
    order = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 1)])
    ok(await client.post(f"{ADMIN_ORDERS}/{order['id']}/status", json={"to_status": "preparing"}, headers=admin_headers))
    detail = ok(await client.get(f"{ORDERS}/{order['id']}", headers=customer_headers))
    assert detail["can_cancel"] is False and detail["status_label"] == "قيد التجهيز"
    assert [(s["status"], s["is_current"], s["reached_at"] is not None) for s in detail["timeline"]] == [
        ("pending", False, True), ("preparing", True, True), ("shipped", False, False), ("delivered", False, False)]
    response = await client.post(f"{ORDERS}/{order['id']}/cancel", headers=customer_headers)
    assert response.status_code == 409 and code_of(response) == "ORDER_NOT_CANCELLABLE"
    assert response.json()["error"]["details"] == {"status": "preparing"}


async def test_reorder(client: AsyncClient, customer_headers: dict, admin_headers: dict, shop: dict, db_session: AsyncSession) -> None:
    order = await order_from_cart(client, customer_headers, [
        (shop["simple"]["id"], None, 2), (shop["lip"]["id"], shop["pink"]["id"], 2), (shop["perfume"]["id"], shop["p50"]["id"], 1),
    ], method="pickup")
    # Afterwards: the perfume is unpublished, pink sold out, cream has only 1 left.
    ok(await client.patch(f"{ADMIN}/products/{shop['perfume']['id']}", json={"is_active": False}, headers=admin_headers))
    await db_session.execute(update(Product).where(Product.id == shop["simple"]["id"]).values(stock=1))
    await db_session.commit()
    result = ok(await client.post(f"{ORDERS}/{order['id']}/reorder", headers=customer_headers))
    report = {(r["product_id"], r["variant_id"]): (r["status"], r["reason"], r["quantity"]) for r in result["merge_report"]}
    assert report[(shop["simple"]["id"], None)] == ("capped", "INSUFFICIENT_STOCK", 1)
    assert report[(shop["lip"]["id"], shop["pink"]["id"])] == ("skipped", "OUT_OF_STOCK", 0)
    assert report[(shop["perfume"]["id"], shop["p50"]["id"])] == ("skipped", "PRODUCT_NOT_FOUND", 0)
    assert [l["product"]["id"] for l in (await cart(client, customer_headers))["lines"]] == [shop["simple"]["id"]]


async def test_deleted_product_after_ordering(
    client: AsyncClient, customer_headers: dict, shop: dict, db_session: AsyncSession
) -> None:
    """Products with orders can't be deleted through the API; simulate it in SQL (FK SET NULL)."""
    order = await order_from_cart(client, customer_headers, [(shop["perfume"]["id"], shop["p50"]["id"], 1),
                                                             (shop["simple"]["id"], None, 1)], method="pickup")
    await db_session.execute(delete(ProductVariant).where(ProductVariant.id == shop["p50"]["id"]))
    await db_session.commit()
    detail = ok(await client.get(f"{ORDERS}/{order['id']}", headers=customer_headers))
    perfume = next(i for i in detail["items"] if i["name"] == "Perfume")
    assert perfume["variant_label"] == "أحمر، 50 ml" and perfume["options"]["size"] == "50 ml"  # snapshot kept
    assert perfume["product_slug"] == shop["perfume"]["slug"]  # the product itself still exists and is visible
    items = (await db_session.scalars(select(OrderItem).where(OrderItem.order_id == order["id"]))).all()
    assert next(i for i in items if i.product_name == "Perfume").variant_id is None

    simple_before = await db_session.scalar(select(Product.stock).where(Product.id == shop["simple"]["id"]))
    ok(await client.post(f"{ORDERS}/{order['id']}/cancel", headers=customer_headers))  # skips the deleted variant
    simple_after = await db_session.scalar(select(Product.stock).where(Product.id == shop["simple"]["id"]).execution_options(populate_existing=True))
    assert simple_after == simple_before + 1

    reorder = ok(await client.post(f"{ORDERS}/{order['id']}/reorder", headers=customer_headers))
    perfume_report = next(r for r in reorder["merge_report"] if r["product_id"] == shop["perfume"]["id"])
    assert (perfume_report["status"], perfume_report["reason"]) == ("skipped", "VARIANT_REQUIRED")

    # And a fully deleted product: no slug, reorder reports product_id null.
    await db_session.execute(update(OrderItem).where(OrderItem.order_id == order["id"], OrderItem.product_name == "Cream").values(product_id=None))
    await db_session.commit()
    detail = ok(await client.get(f"{ORDERS}/{order['id']}", headers=customer_headers))
    cream = next(i for i in detail["items"] if i["name"] == "Cream")
    assert (cream["product_id"], cream["product_slug"]) == (None, None)
    reorder = ok(await client.post(f"{ORDERS}/{order['id']}/reorder", headers=customer_headers))
    assert any(r["product_id"] is None and r["status"] == "skipped" for r in reorder["merge_report"])
