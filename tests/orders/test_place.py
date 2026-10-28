from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.cart.models import CartItem
from app.modules.orders.models import Order, OrderItem, OrderStatusHistory
from app.modules.products.models import Product, ProductVariant
from cart_fixtures import ADMIN, CART, add, build_shop, cart
from catalog_helpers import code_of, ok
from conftest import bearer, register
from order_helpers import ORDERS, address, order_from_cart, place

pytestmark = pytest.mark.anyio


@pytest.fixture
async def shop(client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession) -> dict[str, Any]:
    return await build_shop(client, admin_headers, db_session)


async def stock_of(session: AsyncSession, product_id: int | None = None, variant_id: int | None = None) -> int:
    if variant_id is not None:
        value = await session.scalar(select(ProductVariant.stock).where(ProductVariant.id == variant_id).execution_options(populate_existing=True))
    else:
        value = await session.scalar(select(Product.stock).where(Product.id == product_id).execution_options(populate_existing=True))
    return int(value or 0)


# ------------------------------------------------------------------------- placement


async def test_mixed_currency_order_keeps_separate_totals_and_requires_exact_map(
    client: AsyncClient,
    admin_headers: dict[str, str],
    customer_headers: dict[str, str],
    shop: dict[str, Any],
) -> None:
    ok(
        await client.patch(
            f"{ADMIN}/products/{shop['perfume']['id']}",
            json={"currency": "USD"},
            headers=admin_headers,
        )
    )
    ok(await add(client, customer_headers, shop["simple"]["id"], quantity=2), 201)
    ok(
        await add(
            client,
            customer_headers,
            shop["perfume"]["id"],
            shop["p50"]["id"],
            quantity=1,
        ),
        201,
    )
    destination = await address(client, customer_headers)

    incomplete = await place(
        client,
        customer_headers,
        address_id=destination["id"],
        total={"SYP": "15016.00"},
    )
    assert incomplete.status_code == 409
    assert code_of(incomplete) == "PRICE_CHANGED"
    assert incomplete.json()["error"]["details"]["totals"] == [
        {"currency": "SYP", "amount": "15016.00"},
        {"currency": "USD", "amount": "20.00"},
    ]

    response = await place(client, customer_headers, address_id=destination["id"])
    assert response.status_code == 201, response.text
    order = response.json()
    assert order["totals"] == [
        {
            "currency": "SYP",
            "subtotal": "16.00",
            "discount": "0.00",
            "shipping_fee": "15000.00",
            "total": "15016.00",
            "is_legacy": False,
        },
        {
            "currency": "USD",
            "subtotal": "20.00",
            "discount": "0.00",
            "shipping_fee": "0.00",
            "total": "20.00",
            "is_legacy": False,
        },
    ]
    assert {item["currency"] for item in order["items"]} == {"SYP", "USD"}


async def test_place_mixed_cart_with_snapshots(
    client: AsyncClient, customer_headers: dict, shop: dict, db_session: AsyncSession
) -> None:
    addr = await address(client, customer_headers)
    ok(await add(client, customer_headers, shop["simple"]["id"], quantity=1), 201)
    ok(await add(client, customer_headers, shop["perfume"]["id"], shop["p100"]["id"], quantity=1), 201)
    ok(await add(client, customer_headers, shop["bundle"]["id"], quantity=1), 201)
    response = await place(client, customer_headers, address_id=addr["id"], customer_notes="  بعد الساعة ٥  ", gift_message="   ")
    assert response.status_code == 201, response.text
    order = response.json()

    assert order["id"] >= 1001 and order["status"] == "pending" and order["status_label"] == "تم استلام طلبك"
    assert (order["payment_method"], order["payment_status"]) == ("cod", "pending")
    assert order["delivery"] == {
        "code": "damascus_courier", "name": "التوصيل داخل دمشق",
        "currency": "SYP", "fee_paid_to_carrier": False,
    }
    assert order["shipping_address"] == {
        "label": "البيت", "governorate": "damascus", "area": "المزة",
        "details": "شارع الجلاء، بناء ٣، الطابق الثاني",
        "latitude": None, "longitude": None, "notes": "يرجى الاتصال قبل الوصول",
    }
    assert (order["customer_notes"], order["gift_message"]) == ("بعد الساعة ٥", None)
    assert order["totals"] == [{
        "currency": "SYP", "subtotal": "63.00", "discount": "0.00",
        "shipping_fee": "15000.00", "total": "15063.00", "is_legacy": False,
    }]
    assert order["can_cancel"] is True
    assert [s["status"] for s in order["timeline"]] == ["pending", "preparing", "shipped", "delivered"]
    assert order["timeline"][0]["is_current"] and order["timeline"][0]["reached_at"] and order["timeline"][1]["reached_at"] is None

    items = {i["name"]: i for i in order["items"]}
    assert all(item["currency"] == "SYP" for item in items.values())
    assert (items["Cream"]["unit_price"], items["Cream"]["line_total"], items["Cream"]["variant_label"]) == ("8.00", "8.00", None)
    assert items["Perfume"]["variant_label"] == "أحمر، 100 ml" and items["Perfume"]["image"] == shop["color_images"]["perfume_red"]["urls"]
    assert items["Box"]["bundle_contents"] == [
        {"product_id": shop["simple"]["id"], "variant_id": None, "name": "Cream", "variant_label": None, "quantity": 2},
        {"product_id": shop["lip"]["id"], "variant_id": shop["red"]["id"], "name": "Lipstick", "variant_label": "أحمر", "quantity": 1},
    ]
    assert items["Cream"]["product_slug"] == shop["simple"]["slug"]

    stored = await db_session.scalar(select(Order).where(Order.id == order["id"]))
    me = (await client.get("/api/v1/users/me", headers=customer_headers)).json()
    assert (stored.customer_name, stored.customer_phone) == (me["name"], me["phone"])
    rows = {i.product_name: i for i in (await db_session.scalars(select(OrderItem).where(OrderItem.order_id == order["id"]))).all()}
    assert rows["Perfume"].sku == shop["p100"]["sku"] and rows["Cream"].sku == shop["simple"]["sku"]
    assert rows["Perfume"].image_key == shop["color_images"]["perfume_red"]["image_key"]
    history = (await db_session.scalars(select(OrderStatusHistory).where(OrderStatusHistory.order_id == order["id"]))).all()
    assert [(h.from_status, h.to_status.value, h.changed_by_user_id) for h in history] == [(None, "pending", me["id"])]

    # Stock: cream 5 - 1 - 2 (bundle) = 2; red 5 - 1 (bundle) = 4; p100 4 - 1 = 3.
    assert await stock_of(db_session, shop["simple"]["id"]) == 2
    assert await stock_of(db_session, variant_id=shop["red"]["id"]) == 4
    assert await stock_of(db_session, variant_id=shop["p100"]["id"]) == 3
    assert await db_session.scalar(select(func.count()).select_from(CartItem)) == 0
    assert (await cart(client, customer_headers))["lines"] == []


async def test_summary_recalculated_when_sold_out(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict
) -> None:
    await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 5)])
    admin_view = ok(await client.get(f"{ADMIN}/products/{shop['simple']['id']}", headers=admin_headers))
    assert (admin_view["stock"], admin_view["in_stock"]) == (0, False)
    box = ok(await client.get(f"{ADMIN}/products/{shop['bundle']['id']}", headers=admin_headers))
    assert (box["bundle_stock"], box["in_stock"]) == (0, False)  # the bundle containing it reacted too


async def test_pickup_and_address_rules(
    client: AsyncClient, customer_headers: dict, shop: dict, db_session: AsyncSession
) -> None:
    ok(await add(client, customer_headers, shop["simple"]["id"]), 201)
    addr = await address(client, customer_headers)
    no_address = await place(client, customer_headers, method="home")
    assert no_address.status_code == 422 and code_of(no_address) == "ADDRESS_REQUIRED"
    other_user = bearer((await register(client, 70))["access_token"])
    foreign = (await address(client, other_user))["id"]
    foreign_address = await place(client, customer_headers, method="home", address_id=foreign)
    assert foreign_address.status_code == 404 and code_of(foreign_address) == "ADDRESS_NOT_FOUND"
    aleppo = await address(client, customer_headers, governorate="aleppo", area="الجميلية")
    wrong_governorate = await place(
        client, customer_headers, method="damascus_courier", address_id=aleppo["id"]
    )
    assert wrong_governorate.status_code == 422
    assert code_of(wrong_governorate) == "DELIVERY_METHOD_NOT_AVAILABLE"
    unknown = await place(client, customer_headers, method="drone", total="8.00")
    assert unknown.status_code == 422 and code_of(unknown) == "DELIVERY_METHOD_NOT_FOUND"

    from sqlalchemy import update

    from app.modules.delivery.models import DeliveryMethod

    await db_session.execute(
        update(DeliveryMethod).where(DeliveryMethod.code == "province_shipping").values(is_active=False)
    )
    await db_session.commit()
    inactive = await place(
        client, customer_headers, method="province_shipping", address_id=aleppo["id"], total="8.00"
    )
    assert code_of(inactive) == "DELIVERY_METHOD_NOT_FOUND"

    assert (await place(client, customer_headers, method="pickup", address_id=addr["id"])).status_code == 201


async def test_delivery_fee_below_free_threshold(client: AsyncClient, customer_headers: dict, shop: dict) -> None:
    order = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 2)])
    assert order["totals"] == [{
        "currency": "SYP", "subtotal": "16.00", "discount": "0.00",
        "shipping_fee": "15000.00", "total": "15016.00", "is_legacy": False,
    }]


async def test_empty_cart_and_cart_with_issues(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict
) -> None:
    empty = await place(client, customer_headers, method="pickup", total="0.00")
    assert empty.status_code == 422 and code_of(empty) == "CART_EMPTY"
    ok(await add(client, customer_headers, shop["simple"]["id"]), 201)
    ok(await client.patch(f"{ADMIN}/products/{shop['simple']['id']}", json={"is_active": False}, headers=admin_headers))
    issues = await place(client, customer_headers, method="pickup", total="8.00")
    assert issues.status_code == 409 and code_of(issues) == "CART_HAS_ISSUES"
    assert issues.json()["error"]["details"]["cart"]["lines"][0]["issue"]["code"] == "UNAVAILABLE"


async def test_price_changed(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict, db_session: AsyncSession
) -> None:
    ok(await add(client, customer_headers, shop["simple"]["id"], quantity=2), 201)
    from order_helpers import expected_total

    seen = await expected_total(client, customer_headers, "pickup")  # 16.00
    ok(await client.patch(f"{ADMIN}/products/{shop['simple']['id']}", json={"sale_price": "7.50"}, headers=admin_headers))
    response = await place(client, customer_headers, method="pickup", total=seen)
    assert response.status_code == 409 and code_of(response) == "PRICE_CHANGED"
    details = response.json()["error"]["details"]
    assert details["expected_totals"] == [{"currency": "SYP", "amount": "15016.00"}]
    assert details["totals"] == [{"currency": "SYP", "amount": "15015.00"}]
    assert details["cart"]["subtotals"] == [{"currency": "SYP", "amount": "15.00"}]
    assert await stock_of(db_session, shop["simple"]["id"]) == 5  # nothing saved
    assert await db_session.scalar(select(func.count()).select_from(Order)) == 0
    assert len((await cart(client, customer_headers))["lines"]) == 1


async def test_insufficient_stock_detected_at_placement(
    client: AsyncClient, customer_headers: dict, shop: dict, db_session: AsyncSession
) -> None:
    """The cart was fine, but a component ran out (only the bundle component's stock dropped)."""
    ok(await add(client, customer_headers, shop["simple"]["id"], quantity=1), 201)
    ok(await add(client, customer_headers, shop["bundle"]["id"], quantity=2), 201)  # needs 4 creams -> 5 total
    from sqlalchemy import update

    await db_session.execute(update(Product).where(Product.id == shop["simple"]["id"]).values(stock=4))
    await db_session.commit()
    response = await place(client, customer_headers, method="pickup", total="15058.00")
    assert response.status_code == 409 and code_of(response) == "INSUFFICIENT_STOCK"
    assert response.json()["error"]["details"] == {"product_id": shop["simple"]["id"], "variant_id": None, "available": 4}
    assert await stock_of(db_session, shop["simple"]["id"]) == 4 and await stock_of(db_session, variant_id=shop["red"]["id"]) == 5


# --------------------------------------------------------------------------- idempotency


async def test_idempotency(client: AsyncClient, customer_headers: dict, shop: dict, db_session: AsyncSession) -> None:
    ok(await add(client, customer_headers, shop["simple"]["id"], quantity=1), 201)
    first = await place(client, customer_headers, method="pickup", key="checkout-1")
    assert first.status_code == 201
    again = await place(client, customer_headers, method="pickup", key="checkout-1", total="0.00")
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    assert await stock_of(db_session, shop["simple"]["id"]) == 4  # deducted once

    ok(await add(client, customer_headers, shop["simple"]["id"], quantity=1), 201)
    second = await place(client, customer_headers, method="pickup", key="checkout-2")
    assert second.status_code == 201 and second.json()["id"] != first.json()["id"]
    for bad in ("x" * 65, "has space", "bad!key", ""):
        response = await place(client, customer_headers, method="pickup", key=bad, total="8.00")
        assert response.status_code == 422 and code_of(response) == "VALIDATION_ERROR", bad


async def test_placement_statement_count(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict, count_queries: Any
) -> None:
    """Bounded by the product TYPES in the cart, never by the number of lines."""
    from order_helpers import expected_total

    addr = await address(client, customer_headers)
    ok(await client.post(f"{ADMIN}/products/{shop['simple']['id']}/stock-adjust", json={"delta": 50}, headers=admin_headers))
    for v in ("red", "pink"):
        ok(await client.post(f"{ADMIN}/products/{shop['lip']['id']}/variants/{shop[v]['id']}/stock-adjust", json={"delta": 50}, headers=admin_headers))
    for v in ("p50", "p100"):
        ok(await client.post(f"{ADMIN}/products/{shop['perfume']['id']}/variants/{shop[v]['id']}/stock-adjust", json={"delta": 50}, headers=admin_headers))

    async def place_counted(lines: list[tuple[str, str | None]]) -> int:
        for product, variant in lines:
            ok(await add(client, customer_headers, shop[product]["id"], shop[variant]["id"] if variant else None), 201)
        total = await expected_total(client, customer_headers, "home")
        with count_queries() as counter:
            ok(await place(client, customer_headers, address_id=addr["id"], total=total), 201)
        return counter.count

    one_simple = await place_counted([("simple", None)])
    four_lines = await place_counted([("simple", None), ("lip", "red"), ("perfume", "p50"), ("bundle", None)])
    six_lines = await place_counted([("simple", None), ("lip", "red"), ("lip", "pink"), ("perfume", "p50"),
                                     ("perfume", "p100"), ("bundle", None)])
    import os

    if os.environ.get("ORDER_STATEMENTS_FILE"):
        with open(os.environ["ORDER_STATEMENTS_FILE"], "w", encoding="utf-8") as out:
            out.write(f"1 simple line = {one_simple}, 4 lines (all types) = {four_lines}, 6 lines (all types) = {six_lines}\n")
    assert six_lines == four_lines  # same product types: identical, whatever the number of lines
    assert one_simple <= four_lines <= 25
