from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from cart_fixtures import ADMIN, CART, add, build_shop, cart, line_for, set_setting
from catalog_helpers import code_of, ok

pytestmark = pytest.mark.anyio


@pytest.fixture
async def shop(client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession) -> dict[str, Any]:
    return await build_shop(client, admin_headers, db_session)


def issue_of(data: dict[str, Any], product_id: int, variant_id: int | None = None) -> dict[str, Any] | None:
    issue: dict[str, Any] | None = line_for(data, product_id, variant_id)["issue"]
    return issue


async def adjust(client: AsyncClient, admin: dict, product: dict, delta: int, variant: dict | None = None) -> None:
    url = f"{ADMIN}/products/{product['id']}" + (f"/variants/{variant['id']}" if variant else "") + "/stock-adjust"
    ok(await client.post(url, json={"delta": delta}, headers=admin))


# ------------------------------------------------------------------------------- issues


async def test_every_issue_type_and_resolve(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict, db_session: AsyncSession
) -> None:
    ok(await add(client, customer_headers, shop["simple"]["id"], quantity=4), 201)             # -> stock below quantity
    ok(await add(client, customer_headers, shop["lip"]["id"], shop["red"]["id"], quantity=1), 201)  # -> variant inactive
    ok(await add(client, customer_headers, shop["lip"]["id"], shop["pink"]["id"], quantity=1), 201)  # -> stock 0
    ok(await add(client, customer_headers, shop["perfume"]["id"], shop["p50"]["id"], quantity=4), 201)  # -> max lowered
    ok(await add(client, customer_headers, shop["bundle"]["id"], quantity=1), 201)              # -> product inactive
    before = await cart(client, customer_headers)
    assert before["can_checkout"] is True and before["issues_count"] == 0

    await adjust(client, admin_headers, shop["simple"], -3)  # 2 left, line has 4
    ok(await client.patch(f"{ADMIN}/products/{shop['lip']['id']}/variants/{shop['red']['id']}", json={"is_active": False}, headers=admin_headers))
    await adjust(client, admin_headers, shop["lip"], -2, shop["pink"])
    await set_setting(db_session, "cart_max_quantity_per_line", 3)
    ok(await client.patch(f"{ADMIN}/products/{shop['bundle']['id']}", json={"is_active": False}, headers=admin_headers))

    data = await cart(client, customer_headers)
    assert issue_of(data, shop["simple"]["id"]) == {"code": "INSUFFICIENT_STOCK", "message": issue_of(data, shop["simple"]["id"])["message"], "available": 2, "max": None}
    assert issue_of(data, shop["lip"]["id"], shop["red"]["id"])["code"] == "VARIANT_UNAVAILABLE"
    assert issue_of(data, shop["lip"]["id"], shop["pink"]["id"])["code"] == "OUT_OF_STOCK"
    assert issue_of(data, shop["perfume"]["id"], shop["p50"]["id"]) == {"code": "QUANTITY_LIMIT_EXCEEDED", "message": issue_of(data, shop["perfume"]["id"], shop["p50"]["id"])["message"], "available": None, "max": 3}
    assert issue_of(data, shop["bundle"]["id"])["code"] == "UNAVAILABLE"
    assert all(l["issue"]["message"] for l in data["lines"])
    assert data["lines_count"] == 5 and data["issues_count"] == 5 and data["can_checkout"] is False
    assert data["items_count"] == 0 and data["subtotals"] == []
    assert all(l["line_total"] == "0.00" for l in data["lines"])
    assert data["notices"] == []  # plain read: nothing changed, nothing deleted
    assert (await cart(client, customer_headers))["lines_count"] == 5

    resolved = ok(await client.post(f"{CART}/resolve-issues", headers=customer_headers))
    assert resolved["issues_count"] == 0 and resolved["can_checkout"] is True
    remaining = {(l["product"]["id"], (l["variant"] or {}).get("id")): l["quantity"] for l in resolved["lines"]}
    assert remaining == {(shop["simple"]["id"], None): 2, (shop["perfume"]["id"], shop["p50"]["id"]): 3}
    notices = {(n["code"], n["reason"]) for n in resolved["notices"]}
    assert notices == {
        ("QUANTITY_CAPPED", "INSUFFICIENT_STOCK"), ("QUANTITY_CAPPED", "QUANTITY_LIMIT_EXCEEDED"),
        ("LINE_REMOVED", "VARIANT_UNAVAILABLE"), ("LINE_REMOVED", "OUT_OF_STOCK"), ("LINE_REMOVED", "UNAVAILABLE"),
    }
    assert ok(await client.get(CART, headers=customer_headers))["lines"] == resolved["lines"]  # it was saved


async def test_category_and_brand_deactivation_make_lines_unavailable(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict
) -> None:
    ok(await add(client, customer_headers, shop["simple"]["id"]), 201)
    ok(await client.patch(f"{ADMIN}/brands/{shop['brand']['id']}", json={"is_active": False}, headers=admin_headers))
    assert issue_of(await cart(client, customer_headers), shop["simple"]["id"])["code"] == "UNAVAILABLE"
    ok(await client.patch(f"{ADMIN}/brands/{shop['brand']['id']}", json={"is_active": True}, headers=admin_headers))
    assert issue_of(await cart(client, customer_headers), shop["simple"]["id"]) is None
    ok(await client.patch(f"{ADMIN}/categories/{shop['category']['id']}", json={"is_active": False}, headers=admin_headers))
    data = await cart(client, customer_headers)
    assert issue_of(data, shop["simple"]["id"])["code"] == "UNAVAILABLE" and data["can_checkout"] is False


async def test_bundle_line_reacts_to_component_stock(client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict) -> None:
    ok(await add(client, customer_headers, shop["bundle"]["id"], quantity=2), 201)
    await adjust(client, admin_headers, shop["simple"], -2)  # 3 left -> bundle stock 1
    assert issue_of(await cart(client, customer_headers), shop["bundle"]["id"]) == {
        "code": "INSUFFICIENT_STOCK", "message": "الكمية المتوفرة أقل من المطلوب.", "available": 1, "max": None,
    }


# -------------------------------------------------------------------------------- merge


async def test_merge_statuses_and_reasons(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict, db_session: AsyncSession
) -> None:
    ok(await add(client, customer_headers, shop["simple"]["id"], quantity=1), 201)
    hidden = await client.post(f"{ADMIN}/products", json={"type": "simple", "name": "Hidden", "category_id": shop["category"]["id"], "price": "1", "stock": 1}, headers=admin_headers)
    items = [
        {"product_id": shop["simple"]["id"], "quantity": 2},                                  # 0 merged (1+2=3)
        {"product_id": shop["lip"]["id"], "variant_id": shop["red"]["id"], "quantity": 2},    # 1 added
        {"product_id": shop["lip"]["id"], "variant_id": shop["pink"]["id"], "quantity": 5},   # 2 capped (stock 2)
        {"product_id": shop["perfume"]["id"], "variant_id": shop["p50"]["id"], "quantity": 12},  # 3 capped (max 10 -> stock 4)
        {"product_id": hidden.json()["id"]},                                                  # 4 skipped not visible
        {"product_id": shop["lip"]["id"]},                                                    # 5 skipped variant required
        {"product_id": shop["simple"]["id"], "variant_id": shop["red"]["id"]},                # 6 skipped variant not allowed
        {"product_id": shop["lip"]["id"], "variant_id": shop["p50"]["id"]},                   # 7 skipped variant not found
        {"product_id": shop["lip"]["id"], "variant_id": shop["blue"]["id"]},                  # 8 skipped out of stock
        {"product_id": shop["lip"]["id"], "variant_id": shop["red"]["id"], "quantity": 1},    # 9 merged with item 1
        {"product_id": 999_999},                                                              # 10 skipped unknown
    ]
    data = ok(await client.post(f"{CART}/merge", json={"items": items}, headers=customer_headers))
    report = [(r["index"], r["status"], r["reason"], r["quantity"]) for r in data["merge_report"]]
    assert report == [
        (0, "merged", None, 3),
        (1, "added", None, 2),
        (2, "capped", "INSUFFICIENT_STOCK", 2),
        (3, "capped", "INSUFFICIENT_STOCK", 4),
        (4, "skipped", "PRODUCT_NOT_FOUND", 0),
        (5, "skipped", "VARIANT_REQUIRED", 0),
        (6, "skipped", "VARIANT_NOT_ALLOWED", 0),
        (7, "skipped", "VARIANT_NOT_FOUND", 0),
        (8, "skipped", "OUT_OF_STOCK", 0),
        (9, "merged", None, 3),
        (10, "skipped", "PRODUCT_NOT_FOUND", 0),
    ]
    assert data["lines_count"] == 4 and data["can_checkout"] is True
    assert line_for(data, shop["lip"]["id"], shop["red"]["id"])["quantity"] == 3

    await set_setting(db_session, "cart_max_quantity_per_line", 4)
    capped_by_max = ok(await client.post(f"{CART}/merge", json={"items": [{"product_id": shop["lip"]["id"], "variant_id": shop["red"]["id"], "quantity": 5}]}, headers=customer_headers))
    assert [(r["status"], r["reason"], r["quantity"]) for r in capped_by_max["merge_report"]] == [("capped", "QUANTITY_LIMIT_EXCEEDED", 4)]


async def test_merge_line_limit_and_body_limit(client: AsyncClient, customer_headers: dict, shop: dict, db_session: AsyncSession) -> None:
    await set_setting(db_session, "cart_max_lines", 2)
    data = ok(await client.post(f"{CART}/merge", json={"items": [
        {"product_id": shop["simple"]["id"]},
        {"product_id": shop["lip"]["id"], "variant_id": shop["red"]["id"]},
        {"product_id": shop["perfume"]["id"], "variant_id": shop["p50"]["id"]},
    ]}, headers=customer_headers))
    assert [r["status"] for r in data["merge_report"]] == ["added", "added", "skipped"]
    assert data["merge_report"][2]["reason"] == "CART_LIMIT_REACHED" and data["lines_count"] == 2
    too_big = await client.post(f"{CART}/merge", json={"items": [{"product_id": 1}] * 101}, headers=customer_headers)
    assert too_big.status_code == 422 and code_of(too_big) == "VALIDATION_ERROR"


async def test_merge_is_atomic(
    client: AsyncClient, customer_headers: dict, shop: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_commit() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db_session, "commit", failing_commit)
    response = await client.post(f"{CART}/merge", json={"items": [
        {"product_id": shop["simple"]["id"]}, {"product_id": shop["lip"]["id"], "variant_id": shop["red"]["id"]},
    ]}, headers=customer_headers)
    assert response.status_code == 500
    monkeypatch.undo()
    assert (await cart(client, customer_headers))["lines"] == []
