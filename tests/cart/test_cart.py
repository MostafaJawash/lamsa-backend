from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from cart_fixtures import ADMIN, CART, add, build_shop, cart, line_for, set_setting
from catalog_helpers import code_of, ok
from conftest import bearer, register

pytestmark = pytest.mark.anyio


@pytest.fixture
async def shop(client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession) -> dict[str, Any]:
    return await build_shop(client, admin_headers, db_session)


# ---------------------------------------------------------------------------------- add


async def test_add_simple_line_and_totals(client: AsyncClient, customer_headers: dict, shop: dict) -> None:
    response = await add(client, customer_headers, shop["simple"]["id"], quantity=2)
    assert response.status_code == 201
    data = response.json()
    line = data["lines"][0]
    assert line["product"] == {"id": shop["simple"]["id"], "slug": shop["simple"]["slug"], "name": "Cream", "type": "simple",
                               "brand": {"name": "Cart Brand", "slug": "cart-brand"}}
    assert line["variant"] is None and line["issue"] is None
    assert (line["unit_price"], line["regular_price"], line["compare_at_price"], line["discount_percent"]) == ("8.00", "10.00", "10.00", 20)
    assert (line["line_total"], line["stock_status"], line["stock_left"]) == ("16.00", "in_stock", None)
    assert (data["items_count"], data["lines_count"], data["subtotal"], data["savings"]) == (2, 1, "16.00", "4.00")
    assert data["can_checkout"] is True and data["issues_count"] == 0 and data["notices"] == []

    again = await add(client, customer_headers, shop["simple"]["id"], quantity=1)
    assert again.status_code == 200  # existing line grew
    assert again.json()["lines"][0]["quantity"] == 3 and again.json()["lines_count"] == 1


async def test_add_variable_and_bundle(client: AsyncClient, customer_headers: dict, shop: dict) -> None:
    ok(await add(client, customer_headers, shop["lip"]["id"], shop["pink"]["id"]), 201)
    data = ok(await add(client, customer_headers, shop["perfume"]["id"], shop["p100"]["id"]), 201)
    pink = line_for(data, shop["lip"]["id"], shop["pink"]["id"])
    assert pink["variant"]["label"] == "وردي" and (pink["unit_price"], pink["compare_at_price"]) == ("9.00", "12.00")
    assert (pink["stock_status"], pink["stock_left"]) == ("low", 2)
    p100 = line_for(data, shop["perfume"]["id"], shop["p100"]["id"])
    assert p100["variant"]["label"] == "أحمر، 100 ml" and p100["variant"]["options"]["size"] == "100 ml"

    data = ok(await add(client, customer_headers, shop["bundle"]["id"]), 201)
    box = line_for(data, shop["bundle"]["id"])
    assert box["items"] == [{"name": "Cream", "variant_label": None, "quantity": 2},
                            {"name": "Lipstick", "variant_label": "أحمر", "quantity": 1}]
    assert box["stock_status"] == "low" and box["stock_left"] == 2  # min(5 // 2, 5)


@pytest.mark.parametrize(
    ("product", "variant", "status", "code"),
    [
        ("lip", None, 422, "VARIANT_REQUIRED"),
        ("lip", "p50", 404, "VARIANT_NOT_FOUND"),  # another product's variant
        ("simple", "red", 422, "VARIANT_NOT_ALLOWED"),
        ("bundle", "red", 422, "VARIANT_NOT_ALLOWED"),
        ("lip", "blue", 409, "OUT_OF_STOCK"),
    ],
)
async def test_add_target_rules(client: AsyncClient, customer_headers: dict, shop: dict, product: str, variant: str | None, status: int, code: str) -> None:
    response = await add(client, customer_headers, shop[product]["id"], shop[variant]["id"] if variant else None)
    assert response.status_code == status and code_of(response) == code


async def test_add_inactive_variant_and_invisible_product(client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict) -> None:
    ok(await client.patch(f"{ADMIN}/products/{shop['lip']['id']}/variants/{shop['red']['id']}", json={"is_active": False}, headers=admin_headers))
    inactive = await add(client, customer_headers, shop["lip"]["id"], shop["red"]["id"])
    assert inactive.status_code == 404 and code_of(inactive) == "VARIANT_NOT_FOUND"
    ok(await client.patch(f"{ADMIN}/products/{shop['simple']['id']}", json={"is_active": False}, headers=admin_headers))
    hidden = await add(client, customer_headers, shop["simple"]["id"])
    assert hidden.status_code == 404 and code_of(hidden) == "PRODUCT_NOT_FOUND"
    missing = await add(client, customer_headers, 999_999)
    assert missing.status_code == 404 and code_of(missing) == "PRODUCT_NOT_FOUND"


async def test_quantity_and_stock_limits(client: AsyncClient, customer_headers: dict, shop: dict, db_session: AsyncSession) -> None:
    zero = await add(client, customer_headers, shop["simple"]["id"], quantity=0)
    assert zero.status_code == 422 and code_of(zero) == "VALIDATION_ERROR"
    too_many = await add(client, customer_headers, shop["perfume"]["id"], shop["p50"]["id"], quantity=11)
    assert too_many.status_code == 422 and code_of(too_many) == "QUANTITY_LIMIT_EXCEEDED"
    assert too_many.json()["error"]["details"] == {"max": 10}

    over_stock = await add(client, customer_headers, shop["simple"]["id"], quantity=6)
    assert over_stock.status_code == 409 and code_of(over_stock) == "INSUFFICIENT_STOCK"
    assert over_stock.json()["error"]["details"] == {"available": 5}
    ok(await add(client, customer_headers, shop["simple"]["id"], quantity=4), 201)
    grow = await add(client, customer_headers, shop["simple"]["id"], quantity=2)  # final 6 > 5
    assert code_of(grow) == "INSUFFICIENT_STOCK"
    variant_stock = await add(client, customer_headers, shop["lip"]["id"], shop["pink"]["id"], quantity=3)
    assert code_of(variant_stock) == "INSUFFICIENT_STOCK" and variant_stock.json()["error"]["details"] == {"available": 2}
    bundle_stock = await add(client, customer_headers, shop["bundle"]["id"], quantity=3)
    assert code_of(bundle_stock) == "INSUFFICIENT_STOCK" and bundle_stock.json()["error"]["details"] == {"available": 2}

    await set_setting(db_session, "cart_max_quantity_per_line", 3)
    lowered = await add(client, customer_headers, shop["perfume"]["id"], shop["p50"]["id"], quantity=4)
    assert lowered.json()["error"]["details"] == {"max": 3}


async def test_line_limit(client: AsyncClient, customer_headers: dict, shop: dict, db_session: AsyncSession) -> None:
    await set_setting(db_session, "cart_max_lines", 2)
    ok(await add(client, customer_headers, shop["simple"]["id"]), 201)
    ok(await add(client, customer_headers, shop["lip"]["id"], shop["red"]["id"]), 201)
    full = await add(client, customer_headers, shop["perfume"]["id"], shop["p50"]["id"])
    assert full.status_code == 422 and code_of(full) == "CART_LIMIT_REACHED" and full.json()["error"]["details"] == {"max": 2}
    ok(await add(client, customer_headers, shop["simple"]["id"]))  # growing an existing line is fine


async def test_other_users_item_is_404(client: AsyncClient, customer_headers: dict, shop: dict) -> None:
    item_id = ok(await add(client, customer_headers, shop["simple"]["id"]), 201)["lines"][0]["id"]
    other = bearer((await register(client, 77))["access_token"])
    for response in (
        await client.patch(f"{CART}/items/{item_id}", json={"quantity": 2}, headers=other),
        await client.delete(f"{CART}/items/{item_id}", headers=other),
    ):
        assert response.status_code == 404 and code_of(response) == "CART_ITEM_NOT_FOUND"
    assert (await cart(client, customer_headers))["lines"][0]["quantity"] == 1


# -------------------------------------------------------------------------------- patch


async def test_patch_quantity_and_delete(client: AsyncClient, customer_headers: dict, shop: dict) -> None:
    item_id = ok(await add(client, customer_headers, shop["simple"]["id"]), 201)["lines"][0]["id"]
    assert ok(await client.patch(f"{CART}/items/{item_id}", json={"quantity": 4}, headers=customer_headers))["lines"][0]["quantity"] == 4
    over = await client.patch(f"{CART}/items/{item_id}", json={"quantity": 6}, headers=customer_headers)
    assert code_of(over) == "INSUFFICIENT_STOCK"
    empty = await client.patch(f"{CART}/items/{item_id}", json={}, headers=customer_headers)
    assert empty.status_code == 422 and code_of(empty) == "VALIDATION_ERROR"
    assert ok(await client.delete(f"{CART}/items/{item_id}", headers=customer_headers))["lines"] == []
    ok(await add(client, customer_headers, shop["simple"]["id"]), 201)
    cleared = ok(await client.delete(CART, headers=customer_headers))
    assert cleared["lines"] == [] and cleared["can_checkout"] is False and cleared["subtotal"] == "0.00"


async def test_patch_variant_and_merge_with_cap(client: AsyncClient, customer_headers: dict, shop: dict) -> None:
    red_line = ok(await add(client, customer_headers, shop["lip"]["id"], shop["red"]["id"], quantity=2), 201)["lines"][0]
    switched = ok(await client.patch(f"{CART}/items/{red_line['id']}", json={"variant_id": shop["pink"]["id"]}, headers=customer_headers))
    assert switched["lines"][0]["variant"]["id"] == shop["pink"]["id"] and switched["notices"] == []

    red2 = ok(await add(client, customer_headers, shop["lip"]["id"], shop["red"]["id"], quantity=1), 201)
    red2_id = line_for(red2, shop["lip"]["id"], shop["red"]["id"])["id"]
    # Switch the red line to pink: pink already has 2, total 3 > stock 2: merged and capped.
    merged = ok(await client.patch(f"{CART}/items/{red2_id}", json={"variant_id": shop["pink"]["id"]}, headers=customer_headers))
    assert [l["variant"]["id"] for l in merged["lines"]] == [shop["pink"]["id"]]
    assert merged["lines"][0]["quantity"] == 2
    assert [n["code"] for n in merged["notices"]] == ["LINES_MERGED", "QUANTITY_CAPPED"]
    assert merged["notices"][1]["from_quantity"] == 3 and merged["notices"][1]["to_quantity"] == 2

    wrong = await client.patch(f"{CART}/items/{merged['lines'][0]['id']}", json={"variant_id": shop["p50"]["id"]}, headers=customer_headers)
    assert wrong.status_code == 404 and code_of(wrong) == "VARIANT_NOT_FOUND"
    simple_id = ok(await add(client, customer_headers, shop["simple"]["id"]), 201)["lines"][-1]["id"]
    not_allowed = await client.patch(f"{CART}/items/{simple_id}", json={"variant_id": shop["red"]["id"]}, headers=customer_headers)
    assert not_allowed.status_code == 422 and code_of(not_allowed) == "VARIANT_NOT_ALLOWED"


# --------------------------------------------------------------------- prices, delivery


async def test_live_prices_and_delivery_quotes(client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict, db_session: AsyncSession) -> None:
    ok(await add(client, customer_headers, shop["simple"]["id"], quantity=2), 201)
    quotes = {q["code"]: q for q in (await cart(client, customer_headers))["delivery_quotes"]}
    assert list(quotes) == ["home", "express", "pickup"]
    assert (quotes["home"]["fee"], quotes["home"]["is_free"], quotes["home"]["free_from"], quotes["home"]["amount_to_free_shipping"]) == ("4.95", False, "49.00", "33.00")
    assert (quotes["express"]["fee"], quotes["express"]["amount_to_free_shipping"]) == ("7.95", None)
    assert (quotes["pickup"]["fee"], quotes["pickup"]["is_free"], quotes["pickup"]["requires_address"]) == ("0.00", True, False)

    # Live price change, reflected immediately.
    ok(await client.patch(f"{ADMIN}/products/{shop['simple']['id']}", json={"sale_price": None, "price": "30.00"}, headers=admin_headers))
    data = await cart(client, customer_headers)
    assert (data["subtotal"], data["savings"], data["lines"][0]["compare_at_price"]) == ("60.00", "0.00", None)
    home = next(q for q in data["delivery_quotes"] if q["code"] == "home")
    assert (home["fee"], home["is_free"], home["amount_to_free_shipping"]) == ("0.00", True, None)

    from sqlalchemy import update

    from app.modules.delivery.models import DeliveryMethod

    await db_session.execute(update(DeliveryMethod).where(DeliveryMethod.code == "express").values(is_active=False))
    await db_session.commit()
    assert [q["code"] for q in (await cart(client, customer_headers))["delivery_quotes"]] == ["home", "pickup"]


async def test_line_image_follows_variant_color(client: AsyncClient, customer_headers: dict, shop: dict) -> None:
    ok(await add(client, customer_headers, shop["lip"]["id"], shop["red"]["id"]), 201)
    ok(await add(client, customer_headers, shop["lip"]["id"], shop["pink"]["id"]), 201)
    ok(await add(client, customer_headers, shop["perfume"]["id"], shop["p100"]["id"]), 201)
    data = await cart(client, customer_headers)
    red = line_for(data, shop["lip"]["id"], shop["red"]["id"])
    pink = line_for(data, shop["lip"]["id"], shop["pink"]["id"])
    p100 = line_for(data, shop["perfume"]["id"], shop["p100"]["id"])
    assert red["image"] == shop["color_images"]["lip_red"]["urls"]
    assert pink["image"] == shop["general"][shop["lip"]["id"]]["urls"]  # no pink images: main image
    # The red image was uploaded for the 50 ml variant; it serves every red variant.
    assert p100["image"] == shop["color_images"]["perfume_red"]["urls"]
