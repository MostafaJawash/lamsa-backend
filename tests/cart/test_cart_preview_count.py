"""POST /cart/preview (guests: the cart kept on the device) and GET /cart/count (badges)."""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from cart_fixtures import ADMIN, CART, add, build_shop, cart, set_setting
from catalog_helpers import code_of, create_product, ok
from conftest import bearer

pytestmark = pytest.mark.anyio

PREVIEW = f"{CART}/preview"
COUNT = f"{CART}/count"


@pytest.fixture
async def shop(client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession) -> dict[str, Any]:
    return await build_shop(client, admin_headers, db_session)


def item(product_id: int, variant_id: int | None = None, quantity: int = 1) -> dict[str, Any]:
    return {"product_id": product_id, "variant_id": variant_id, "quantity": quantity}


async def preview(client: AsyncClient, items: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    data: dict[str, Any] = ok(await client.post(PREVIEW, json={"items": items}, **kwargs))
    return data


def without_line_ids(data: dict[str, Any]) -> dict[str, Any]:
    return {**data, "lines": [{k: v for k, v in line.items() if k not in ("id", "key")} for line in data["lines"]]}


async def adjust(client: AsyncClient, admin: dict[str, str], product: dict[str, Any], delta: int, variant: Any = None) -> None:
    path = f"{ADMIN}/products/{product['id']}" + (f"/variants/{variant['id']}" if variant else "") + "/stock-adjust"
    ok(await client.post(path, json={"delta": delta}, headers=admin))


# ------------------------------------------------------------------------------- preview


async def test_preview_equals_the_real_cart_with_issues(
    client: AsyncClient, admin_headers: dict[str, str], customer_headers: dict[str, str], shop: dict[str, Any]
) -> None:
    wanted = [
        (shop["simple"]["id"], None, 2), (shop["lip"]["id"], shop["red"]["id"], 3),
        (shop["lip"]["id"], shop["pink"]["id"], 2), (shop["bundle"]["id"], None, 1),
        (shop["perfume"]["id"], shop["p50"]["id"], 1),
    ]
    for product_id, variant_id, quantity in wanted:
        assert (await add(client, customer_headers, product_id, variant_id, quantity)).status_code == 201
    await adjust(client, admin_headers, shop["lip"], -1, shop["pink"])  # INSUFFICIENT_STOCK (1 left)
    await adjust(client, admin_headers, shop["perfume"], -4, shop["p50"])  # OUT_OF_STOCK

    real = await cart(client, customer_headers)
    guest = await preview(client, [item(p, v, q) for p, v, q in wanted])
    assert without_line_ids(guest) == without_line_ids(real)
    assert [line["key"] for line in guest["lines"]] == [f"{p}:{v or 0}" for p, v, _ in wanted]
    assert "id" not in guest["lines"][0]
    assert {line["issue"]["code"] for line in guest["lines"] if line["issue"]} == {"INSUFFICIENT_STOCK", "OUT_OF_STOCK"}
    assert guest["can_checkout"] is False and guest["delivery_quotes"]
    assert guest["lines"][3]["items"]  # bundle contents


async def test_preview_unknown_hidden_and_wrong_variants_are_unavailable_lines(
    client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, Any]
) -> None:
    draft = await create_product(client, admin_headers, name="Draft", category_id=shop["category"]["id"], price="3")
    data = await preview(client, [
        item(999999), item(draft["id"]), item(shop["lip"]["id"]), item(shop["lip"]["id"], shop["p50"]["id"]),
        item(shop["simple"]["id"], shop["red"]["id"]), item(shop["lip"]["id"], shop["blue"]["id"]),
        item(shop["simple"]["id"], None, 1),
    ])
    codes = [(line["key"], line["issue"]["code"] if line["issue"] else None) for line in data["lines"]]
    lip, simple = shop["lip"]["id"], shop["simple"]["id"]
    assert codes == [
        ("999999:0", "UNAVAILABLE"), (f"{draft['id']}:0", "UNAVAILABLE"), (f"{lip}:0", "VARIANT_UNAVAILABLE"),
        (f"{lip}:{shop['p50']['id']}", "VARIANT_UNAVAILABLE"), (f"{simple}:{shop['red']['id']}", "UNAVAILABLE"),
        (f"{lip}:{shop['blue']['id']}", "OUT_OF_STOCK"), (f"{simple}:0", None),
    ]
    unknown = data["lines"][0]
    assert unknown["product"]["id"] == 999999 and unknown["product"]["name"] == "" and unknown["line_total"] == "0.00"
    assert data["lines"][1]["product"]["name"] == "Draft"
    assert (data["items_count"], data["lines_count"], data["issues_count"]) == (1, 7, 6)
    assert data["subtotals"] == [{"currency": "SYP", "amount": "8.00"}]


async def test_preview_quantity_issues_and_duplicates(
    client: AsyncClient, shop: dict[str, Any], db_session: AsyncSession
) -> None:
    data = await preview(client, [item(shop["simple"]["id"], None, 6), item(shop["lip"]["id"], shop["pink"]["id"], 3)])
    assert [(l["issue"]["code"], l["issue"]["available"]) for l in data["lines"]] == [
        ("INSUFFICIENT_STOCK", 5), ("INSUFFICIENT_STOCK", 2)
    ]
    # The same product and variant twice: one line, capped at the per-line maximum (10).
    data = await preview(client, [item(shop["simple"]["id"], None, 7), item(shop["simple"]["id"], None, 7)])
    assert [(l["key"], l["quantity"]) for l in data["lines"]] == [(f"{shop['simple']['id']}:0", 10)]
    assert [n["code"] for n in data["notices"]] == ["LINES_MERGED", "QUANTITY_CAPPED"]
    await set_setting(db_session, "cart_max_quantity_per_line", 2)
    data = await preview(client, [item(shop["lip"]["id"], shop["red"]["id"], 3)])
    assert (data["lines"][0]["issue"]["code"], data["lines"][0]["issue"]["max"]) == ("QUANTITY_LIMIT_EXCEEDED", 2)


@pytest.mark.parametrize(
    "items",
    [[item(1, None, 0)], [item(1, None, 100)], [item(0)], [item(1, 0)], [item(1)] * 51, [{"variant_id": 1, "quantity": 1}]],
)
async def test_preview_validation(client: AsyncClient, items: list[dict[str, Any]]) -> None:
    response = await client.post(PREVIEW, json={"items": items})
    assert response.status_code == 422 and code_of(response) == "VALIDATION_ERROR"


async def test_preview_is_public_stores_nothing_and_ignores_the_token(
    client: AsyncClient, customer_headers: dict[str, str], shop: dict[str, Any]
) -> None:
    assert (await add(client, customer_headers, shop["simple"]["id"])).status_code == 201
    before = await cart(client, customer_headers)
    data = await preview(client, [item(shop["perfume"]["id"], shop["p100"]["id"], 2)], headers=customer_headers)
    assert [l["product"]["id"] for l in data["lines"]] == [shop["perfume"]["id"]]  # not the user's cart
    assert ok(await client.post(PREVIEW, json={"items": []}, headers=bearer("garbage")))["lines"] == []
    assert await cart(client, customer_headers) == before
    empty = await preview(client, [])
    assert (empty["lines"], empty["can_checkout"], empty["items_count"]) == ([], False, 0)


async def test_preview_query_budget_does_not_grow_with_items(
    client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, Any], count_queries: Any
) -> None:
    extra = [await create_product(client, admin_headers, name=f"P{i}", category_id=shop["category"]["id"], price="1")
             for i in range(20)]
    small = [item(shop["simple"]["id"])]
    large = [item(p["id"]) for p in extra] + [
        item(shop["lip"]["id"], shop["red"]["id"]), item(shop["lip"]["id"], shop["pink"]["id"]),
        item(shop["perfume"]["id"], shop["p50"]["id"]), item(shop["bundle"]["id"]), item(shop["simple"]["id"]),
    ]
    await preview(client, small)  # warms the per-process delivery methods copy
    with count_queries() as one:
        await preview(client, small)
    with count_queries() as many:
        await preview(client, large)
    assert one.count <= 3, one.statements
    assert many.count <= 4, many.statements


# --------------------------------------------------------------------------------- count


async def test_count_matches_the_cart(
    client: AsyncClient, admin_headers: dict[str, str], customer_headers: dict[str, str], shop: dict[str, Any]
) -> None:
    assert ok(await client.get(COUNT, headers=customer_headers)) == {"items_count": 0, "lines_count": 0}
    await add(client, customer_headers, shop["simple"]["id"], None, 2)
    await add(client, customer_headers, shop["lip"]["id"], shop["pink"]["id"], 2)
    await add(client, customer_headers, shop["bundle"]["id"])
    await adjust(client, admin_headers, shop["lip"], -1, shop["pink"])  # that line gets an issue
    real = await cart(client, customer_headers)
    counted = ok(await client.get(COUNT, headers=customer_headers))
    assert counted == {"items_count": real["items_count"], "lines_count": real["lines_count"]} == {
        "items_count": 3, "lines_count": 3
    }


async def test_count_query_budget(
    client: AsyncClient, customer_headers: dict[str, str], shop: dict[str, Any], count_queries: Any
) -> None:
    with count_queries() as empty:
        ok(await client.get(COUNT, headers=customer_headers))
    await add(client, customer_headers, shop["simple"]["id"])
    await add(client, customer_headers, shop["lip"]["id"], shop["red"]["id"])
    with count_queries() as plain:
        ok(await client.get(COUNT, headers=customer_headers))
    await add(client, customer_headers, shop["bundle"]["id"])
    with count_queries() as with_bundle:
        ok(await client.get(COUNT, headers=customer_headers))
    assert (empty.count, plain.count) == (2, 2), (empty.statements, plain.statements)
    assert with_bundle.count == 3, with_bundle.statements
