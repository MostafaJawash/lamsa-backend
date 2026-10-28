"""catalog_hide_out_of_stock: out-of-stock products are left out of browsing; the admin settings API.
Search (FULLTEXT needs committed rows) is covered in test_search.py."""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.products.summary import recalculate_product_summary
from app.modules.settings.admin import ADMIN_SETTINGS
from app.modules.settings.schemas import AdminSettingsOut, AdminSettingsUpdate
from app.shared.events import STASH_KEY, CatalogChanged
from cart_fixtures import build_shop
from catalog_helpers import ADMIN, PUBLIC, code_of, create_product, ok
from conftest import bearer, register
from order_helpers import order_from_cart

pytestmark = pytest.mark.anyio

SETTINGS = f"{ADMIN}/settings"


@pytest.fixture
async def shop(client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession) -> dict[str, Any]:
    return await build_shop(client, admin_headers, db_session)


async def set_hide(client: AsyncClient, admin_headers: dict[str, str], value: bool) -> None:
    assert ok(await client.patch(SETTINGS, json={"catalog_hide_out_of_stock": value}, headers=admin_headers)) == {
        "catalog_hide_out_of_stock": value
    }


async def sell_out_cream(client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, Any]) -> None:
    """Cream (stock 5) sold out; the Box bundle (2 x Cream) with it."""
    ok(await client.post(f"{ADMIN}/products/{shop['simple']['id']}/stock-adjust", json={"delta": -5}, headers=admin_headers))


def ids_of(page: Any) -> set[int]:
    items = page["items"] if isinstance(page, dict) else page
    return {p["id"] for p in items}


# ------------------------------------------------------------------------- admin settings


def test_registry_and_schemas_list_the_same_keys() -> None:
    assert set(ADMIN_SETTINGS) == set(AdminSettingsOut.model_fields) == set(AdminSettingsUpdate.model_fields)


async def test_admin_settings_read_and_change(client: AsyncClient, admin_headers: dict[str, str]) -> None:
    assert ok(await client.get(SETTINGS, headers=admin_headers)) == {"catalog_hide_out_of_stock": True}  # default
    await set_hide(client, admin_headers, False)
    assert ok(await client.get(SETTINGS, headers=admin_headers)) == {"catalog_hide_out_of_stock": False}
    await set_hide(client, admin_headers, True)
    assert ok(await client.get(SETTINGS, headers=admin_headers)) == {"catalog_hide_out_of_stock": True}


@pytest.mark.parametrize(
    "body",
    [{}, {"catalog_hide_out_of_stock": None}, {"catalog_hide_out_of_stock": "false"}, {"catalog_hide_out_of_stock": 0},
     {"store_name": "x"}, {"catalog_hide_out_of_stock": True, "cart_max_lines": 3}],
)
async def test_admin_settings_validation(client: AsyncClient, admin_headers: dict[str, str], body: Any) -> None:
    response = await client.patch(SETTINGS, json=body, headers=admin_headers)
    assert response.status_code == 422 and code_of(response) == "VALIDATION_ERROR"


async def test_admin_settings_need_an_admin(client: AsyncClient, customer_headers: dict[str, str]) -> None:
    for method in ("GET", "PATCH"):
        anonymous = await client.request(method, SETTINGS, json={"catalog_hide_out_of_stock": False})
        assert anonymous.status_code == 401
        customer = await client.request(method, SETTINGS, json={"catalog_hide_out_of_stock": False}, headers=customer_headers)
        assert customer.status_code == 403 and code_of(customer) == "ADMIN_ONLY"


# --------------------------------------------------------------------------- browsing


@pytest.mark.parametrize("hide", [True, False])
async def test_browsing_endpoints_follow_the_setting(
    client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, Any], hide: bool
) -> None:
    await sell_out_cream(client, admin_headers, shop)
    await set_hide(client, admin_headers, hide)
    cream, box, lip = shop["simple"]["id"], shop["bundle"]["id"], shop["lip"]["id"]
    out = {cream, box}

    lists = [
        "/products", "/products?category=cart-shop", "/products?brand=cart-brand",
        "/products?on_sale=true", "/products?sort=price_asc", "/products?page_size=2&page=1",
    ]
    for path in lists:
        shown = ids_of(ok(await client.get(f"{PUBLIC}{path}")))
        assert (shown & out == set()) if hide else (cream in shown or path.endswith("page=1")), path
    everything = ids_of(ok(await client.get(f"{PUBLIC}/products")))
    assert everything == ({lip, shop["perfume"]["id"]} if hide else {cream, box, lip, shop["perfume"]["id"]})
    total = ok(await client.get(f"{PUBLIC}/products", params={"page_size": 1, "page": 2}))["total"]
    assert total == (2 if hide else 4)

    # Facets: counted on in-stock products only (Cream is Cart Brand's, 8.00 the lowest price).
    facets = ok(await client.get(f"{PUBLIC}/products/facets", params={"category": "cart-shop"}))
    assert {b["slug"]: b["count"] for b in facets["brands"]} == {"cart-brand": 1 if hide else 2}
    syp_price = next((row for row in facets["prices"] if row["currency"] == "SYP"), None)
    assert (syp_price is not None and syp_price["min"] == "8.00") is not hide
    assert {b["slug"]: b["count"] for b in ok(await client.get(f"{PUBLIC}/products/facets"))["brands"]} == {
        "cart-brand": 1 if hide else 2
    }

    # product_count of categories and brands.
    tree = ok(await client.get(f"{PUBLIC}/categories"))
    assert next(c for c in tree if c["slug"] == "cart-shop")["product_count"] == (2 if hide else 4)
    assert ok(await client.get(f"{PUBLIC}/categories/cart-shop"))["product_count"] == (2 if hide else 4)
    brands = ok(await client.get(f"{PUBLIC}/brands"))
    assert next(b for b in brands if b["slug"] == "cart-brand")["product_count"] == (1 if hide else 2)
    in_category = ok(await client.get(f"{PUBLIC}/brands", params={"category": "cart-shop"}))
    assert [(b["slug"], b["product_count"]) for b in in_category] == [("cart-brand", 1 if hide else 2)]
    assert ok(await client.get(f"{PUBLIC}/brands/cart-brand"))["product_count"] == (1 if hide else 2)

    # Card swatches: only colors in stock (Lipstick's blue has stock 0).
    card = next(p for p in ok(await client.get(f"{PUBLIC}/products"))["items"] if p["id"] == lip)
    assert [s["name"] for s in card["swatches"]] == (["أحمر", "وردي"] if hide else ["أحمر", "أزرق", "وردي"])

    # Not affected: the product page, ?ids= and related products (always in stock only).
    detail = ok(await client.get(f"{PUBLIC}/products/{shop['simple']['slug']}"))
    assert (detail["in_stock"], detail["stock_status"]) == (False, "out")
    by_ids = ok(await client.get(f"{PUBLIC}/products", params={"ids": f"{cream},{box},{lip}"}))
    assert [p["id"] for p in by_ids["items"]] == [cream, box, lip]
    related = ok(await client.get(f"{PUBLIC}/products/{shop['lip']['slug']}/related"))
    assert ids_of(related) & out == set()


async def test_setting_hides_when_missing_and_categories_with_only_sold_out_products_count_zero(
    client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, Any]
) -> None:
    """No row in store_settings: the default (true) applies."""
    await sell_out_cream(client, admin_headers, shop)
    ok(await client.post(f"{ADMIN}/products/{shop['lip']['id']}/variants/{shop['red']['id']}/stock-adjust",
                         json={"delta": -5}, headers=admin_headers))
    ok(await client.post(f"{ADMIN}/products/{shop['lip']['id']}/variants/{shop['pink']['id']}/stock-adjust",
                         json={"delta": -2}, headers=admin_headers))
    for v in (shop["p50"], shop["p100"]):
        ok(await client.post(f"{ADMIN}/products/{shop['perfume']['id']}/variants/{v['id']}/stock-adjust",
                             json={"delta": -4}, headers=admin_headers))
    assert ok(await client.get(f"{PUBLIC}/products", params={"category": "cart-shop"}))["items"] == []
    assert ok(await client.get(f"{PUBLIC}/categories/cart-shop"))["product_count"] == 0  # the category still exists


# ---------------------------------------------------------------------- invalidation


async def test_changing_the_setting_invalidates_cached_lists(
    client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, Any]
) -> None:
    await sell_out_cream(client, admin_headers, shop)
    assert shop["simple"]["id"] not in ids_of(ok(await client.get(f"{PUBLIC}/products")))  # cached now
    await set_hide(client, admin_headers, False)
    assert shop["simple"]["id"] in ids_of(ok(await client.get(f"{PUBLIC}/products")))
    await set_hide(client, admin_headers, True)
    assert shop["simple"]["id"] not in ids_of(ok(await client.get(f"{PUBLIC}/products")))


async def test_orders_selling_out_and_cancelling_update_cached_lists(
    client: AsyncClient, shop: dict[str, Any], customer_headers: dict[str, str]
) -> None:
    """No admin write involved: the summary recalculation bumps the catalog on 0 <-> >0."""
    cream = shop["simple"]["id"]
    assert cream in ids_of(ok(await client.get(f"{PUBLIC}/products")))
    order = await order_from_cart(client, customer_headers, [(cream, None, 5)], method="pickup")
    assert cream not in ids_of(ok(await client.get(f"{PUBLIC}/products")))
    assert ok(await client.get(f"{PUBLIC}/categories/cart-shop"))["product_count"] == 2  # Cream and Box gone
    ok(await client.post(f"/api/v1/orders/{order['id']}/cancel", headers=customer_headers))  # stock restored
    assert cream in ids_of(ok(await client.get(f"{PUBLIC}/products")))
    assert ok(await client.get(f"{PUBLIC}/categories/cart-shop"))["product_count"] == 4


async def test_summary_bumps_the_catalog_only_when_in_stock_flips(
    client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession
) -> None:
    from app.modules.products.models import Product
    from catalog_helpers import create_category, published_product

    category = await create_category(client, admin_headers, "Flip", slug="flip")
    product = await published_product(client, admin_headers, name="Flip", category_id=category["id"], price="5", stock=10)
    row = await db_session.get(Product, product["id"])
    assert row is not None

    async def stashed_after(stock: int) -> list[Any]:
        row.stock = stock
        db_session.info.pop(STASH_KEY, None)
        await recalculate_product_summary(db_session, [product["id"]])
        events = [e for e in db_session.info.get(STASH_KEY, []) if isinstance(e, CatalogChanged)]
        await db_session.commit()
        return events

    assert await stashed_after(9) == []  # 10 -> 9
    assert len(await stashed_after(0)) == 1  # sold out
    assert await stashed_after(0) == []
    assert len(await stashed_after(3)) == 1  # back in stock


# --------------------------------------------------------------------------- ?ids=


async def test_products_by_ids(
    client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, Any]
) -> None:
    cream, lip, perfume, box = (shop[k]["id"] for k in ("simple", "lip", "perfume", "bundle"))
    draft = await create_product(client, admin_headers, name="Draft", category_id=shop["category"]["id"])
    path = f"{PUBLIC}/products"

    page = ok(await client.get(path, params={"ids": f"{perfume},{cream},{draft['id']},999999,{lip}"}))
    assert [p["id"] for p in page["items"]] == [perfume, cream, lip]  # order kept, hidden/unknown left out
    assert page["total"] == 3
    # Other filters are ignored; a sort applies; paging applies.
    page = ok(await client.get(path, params={"ids": f"{perfume},{cream}", "category": "nope", "q": "zzz"}))
    assert [p["id"] for p in page["items"]] == [perfume, cream]
    page = ok(await client.get(path, params={"ids": f"{perfume},{cream},{lip}", "sort": "price_asc"}))
    assert [p["id"] for p in page["items"]] == [cream, lip, perfume]
    page = ok(await client.get(path, params={"ids": f"{perfume},{cream},{lip},{box}", "page_size": 2, "page": 2}))
    assert ([p["id"] for p in page["items"]], page["total"]) == ([lip, box], 4)
    # Different ids (or order) never share a cache entry with each other or with the plain list.
    assert [p["id"] for p in ok(await client.get(path, params={"ids": f"{cream},{perfume}"}))["items"]] == [cream, perfume]
    assert len(ok(await client.get(path))["items"]) == 4
    # is_favorite is overlaid for a logged-in user.
    user = bearer((await register(client, 61))["access_token"])
    assert (await client.put(f"/api/v1/favorites/{cream}", headers=user)).status_code == 204
    items = ok(await client.get(path, params={"ids": f"{cream},{lip}"}, headers=user))["items"]
    assert [p["is_favorite"] for p in items] == [True, False]


@pytest.mark.parametrize("ids", ["", "a", "1,,2", "0", "-1", "1.5", ",".join(str(i) for i in range(1, 102))])
async def test_products_by_ids_validation(client: AsyncClient, ids: str) -> None:
    response = await client.get(f"{PUBLIC}/products", params={"ids": ids})
    assert response.status_code == 422 and code_of(response) == "INVALID_FILTER"


async def test_products_by_ids_query_budget(client: AsyncClient, shop: dict[str, Any], count_queries: Any) -> None:
    ids = ",".join(str(shop[k]["id"]) for k in ("simple", "lip", "perfume", "bundle"))
    with count_queries() as counter:
        ok(await client.get(f"{PUBLIC}/products", params={"ids": ids}))
    assert counter.count <= 4, counter.statements
    with count_queries() as again:
        ok(await client.get(f"{PUBLIC}/products", params={"ids": ids}))
    assert again.count == 0  # cached
