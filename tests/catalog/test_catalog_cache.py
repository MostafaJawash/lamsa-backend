"""Catalog caching through the API (in-memory backend): hits, key normalization, invalidation after
every kind of admin change, stock freshness of product pages, is_favorite, resilience, headers."""

import logging
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import Cache, MemoryBackend
from cart_fixtures import FAVORITES, build_shop
from catalog_helpers import ADMIN, PUBLIC, ok, png
from conftest import bearer, register
from order_helpers import order_from_cart

pytestmark = pytest.mark.anyio


@pytest.fixture
async def shop(client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession) -> dict[str, Any]:
    return await build_shop(client, admin_headers, db_session)


async def statements(client: AsyncClient, count_queries: Any, path: str, **kwargs: Any) -> tuple[int, Any]:
    with count_queries() as counter:
        response = await client.get(path, **kwargs)
    assert response.status_code == 200, response.text
    return counter.count, response.json()


PUBLIC_PATHS = ["/categories", "/categories/cart-shop", "/brands", "/brands?category=cart-shop", "/brands/cart-brand",
                "/banners", "/products", "/products/facets?category=cart-shop", "/products/cream", "/products/cream/related"]


async def test_second_anonymous_request_is_a_hit_with_zero_statements(
    client: AsyncClient, shop: dict, count_queries: Any
) -> None:
    for path in PUBLIC_PATHS:
        first, body = await statements(client, count_queries, PUBLIC + path)
        second, again = await statements(client, count_queries, PUBLIC + path)
        assert first > 0 and second == 0 and again == body, path


async def test_query_normalization(client: AsyncClient, shop: dict, count_queries: Any) -> None:
    base = f"{PUBLIC}/products"
    await statements(client, count_queries, f"{base}?brand=cart-brand&in_stock=true&min_price=5&page=1")
    for same in (
        "?in_stock=1&min_price=5.00&brand=cart-brand",  # other order, other spellings, default page
        "?brand=cart-brand&brand=cart-brand&in_stock=yes&min_price=5&page_size=20",  # repeated value, default size
        "?brand=cart-brand&in_stock=true&min_price=5&utm_source=push&on_sale=false",  # ignored / default params
    ):
        count, _ = await statements(client, count_queries, base + same)
        assert count == 0, same
    different, _ = await statements(client, count_queries, f"{base}?brand=cart-brand&in_stock=true&min_price=5&page=2")
    assert different > 0


async def _mutations(client: AsyncClient, admin: dict[str, str], shop: dict) -> list[tuple[str, Any]]:
    """One admin change of each kind; each must invalidate every catalog entry."""
    cat, brand, cream, lip, bundle = shop["category"], shop["brand"], shop["simple"], shop["lip"], shop["bundle"]
    image = shop["general"][cream["id"]]
    return [
        ("category", lambda: client.patch(f"{ADMIN}/categories/{cat['id']}", json={"name": "Cart Shop 2"}, headers=admin)),
        ("definition", lambda: client.post(f"{ADMIN}/categories/{cat['id']}/attributes",
                                           json={"key": "finish", "label": "اللمسة", "type": "text"}, headers=admin)),
        ("brand", lambda: client.patch(f"{ADMIN}/brands/{brand['id']}", json={"name": "Cart Brand 2"}, headers=admin)),
        ("product", lambda: client.patch(f"{ADMIN}/products/{cream['id']}", json={"name": "Cream 2"}, headers=admin)),
        ("variant", lambda: client.patch(f"{ADMIN}/products/{lip['id']}/variants/{shop['red']['id']}",
                                         json={"price": "13.00"}, headers=admin)),
        ("bundle items", lambda: client.put(f"{ADMIN}/products/{bundle['id']}/bundle-items",
                                            json={"items": [{"product_id": cream["id"], "quantity": 3}]}, headers=admin)),
        ("image", lambda: client.patch(f"{ADMIN}/products/{cream['id']}/images/{image['id']}",
                                       json={"alt_text": "new"}, headers=admin)),
        ("banner", lambda: client.post(f"{ADMIN}/banners", data={"title": "b"},
                                       files={"image": ("b.png", png(), "image/png")}, headers=admin)),
        ("stock adjust", lambda: client.post(f"{ADMIN}/products/{cream['id']}/stock-adjust", json={"delta": 1}, headers=admin)),
    ]


async def test_every_kind_of_admin_change_invalidates(
    client: AsyncClient, admin_headers: dict, shop: dict, count_queries: Any
) -> None:
    for kind, mutate in await _mutations(client, admin_headers, shop):
        for path in PUBLIC_PATHS:  # warm everything
            await statements(client, count_queries, PUBLIC + path)
        response = await mutate()
        assert response.status_code in (200, 201), (kind, response.text)
        for path in PUBLIC_PATHS:
            count, _ = await statements(client, count_queries, PUBLIC + path)
            assert count > 0, (kind, path)  # recomputed, not served from the old entry


async def test_failed_admin_change_does_not_invalidate(
    client: AsyncClient, admin_headers: dict, shop: dict, count_queries: Any
) -> None:
    await statements(client, count_queries, f"{PUBLIC}/products")
    rejected = await client.patch(f"{ADMIN}/products/{shop['simple']['id']}", json={"price": "-1"}, headers=admin_headers)
    assert rejected.status_code == 422
    count, _ = await statements(client, count_queries, f"{PUBLIC}/products")
    assert count == 0


async def test_product_page_and_lists_fresh_after_last_unit_sold(
    client: AsyncClient, customer_headers: dict, shop: dict, count_queries: Any
) -> None:
    cream = shop["simple"]
    _, detail = await statements(client, count_queries, f"{PUBLIC}/products/{cream['slug']}")
    _, listing = await statements(client, count_queries, f"{PUBLIC}/products")
    assert detail["in_stock"] is True
    await order_from_cart(client, customer_headers, [(cream["id"], None, 5)], method="pickup")  # all 5 units

    _, detail = await statements(client, count_queries, f"{PUBLIC}/products/{cream['slug']}")
    assert (detail["in_stock"], detail["stock_status"]) == (False, "out")  # fresh right away
    # Sold out: the catalog version is bumped, so lists are recomputed and (default setting
    # catalog_hide_out_of_stock) no longer show it.
    count, fresh = await statements(client, count_queries, f"{PUBLIC}/products")
    assert count > 0 and fresh != listing
    assert cream["id"] in [p["id"] for p in listing["items"]]
    assert cream["id"] not in [p["id"] for p in fresh["items"]]
    bundle_count, bundle = await statements(client, count_queries, f"{PUBLIC}/products/{shop['bundle']['slug']}")
    assert bundle_count > 0 and bundle["in_stock"] is False  # the bundle containing it too


async def test_is_favorite_is_never_cached_and_correct_per_user(
    client: AsyncClient, shop: dict, memory_cache: Cache
) -> None:
    cream = shop["simple"]
    alice = bearer((await register(client, 20, name="Alice"))["access_token"])
    bob = bearer((await register(client, 21, name="Bob"))["access_token"])
    assert (await client.put(f"{FAVORITES}/{cream['id']}", headers=alice)).status_code == 204

    def flag(data: Any) -> bool:
        cards = data["items"] if isinstance(data, dict) and "page" in data else data if isinstance(data, list) else [data]
        return next(c["is_favorite"] for c in cards if c["id"] == cream["id"])

    for path in ("/products", f"/products/{cream['slug']}", f"/products/{shop['bundle']['slug']}/related"):
        for _ in range(2):  # miss, then hit
            assert flag(ok(await client.get(PUBLIC + path, headers=alice))) is True, path
            assert flag(ok(await client.get(PUBLIC + path, headers=bob))) is False, path
            assert flag(ok(await client.get(PUBLIC + path))) is False, path
    backend = memory_cache.backend
    assert isinstance(backend, MemoryBackend)
    stored = [value for key, (value, _) in backend.data.items() if ":c:" in key]
    assert stored and all(b'"is_favorite":true' not in value for value in stored)


async def test_cache_down_everything_still_works(
    client: AsyncClient, shop: dict, memory_cache: Cache, caplog: pytest.LogCaptureFixture
) -> None:
    warm = {path: ok(await client.get(PUBLIC + path)) for path in PUBLIC_PATHS}
    backend = memory_cache.backend
    assert isinstance(backend, MemoryBackend)
    backend.fail = True
    with caplog.at_level(logging.WARNING, logger="app.cache"):
        for path in PUBLIC_PATHS:
            response = await client.get(PUBLIC + path)
            assert response.status_code == 200 and response.json() == warm[path], path
    assert len([r for r in caplog.records if "Cache unavailable" in r.message]) == 1


async def test_http_caching_headers(
    client: AsyncClient, customer_headers: dict, shop: dict
) -> None:
    anonymous = await client.get(f"{PUBLIC}/products")
    assert anonymous.headers["cache-control"] == "public, max-age=45, stale-while-revalidate=60"
    assert "Authorization" in anonymous.headers["vary"]  # (CORS adds Origin)
    etag = anonymous.headers["etag"]
    assert etag.startswith('W/"')
    not_modified = await client.get(f"{PUBLIC}/products", headers={"If-None-Match": etag})
    assert not_modified.status_code == 304 and not_modified.content == b"" and not_modified.headers["etag"] == etag
    assert (await client.get(f"{PUBLIC}/products", headers={"If-None-Match": 'W/"other"'})).status_code == 200

    for path, ttl in (("/categories", 300), ("/brands", 300), ("/banners", 60), (f"/products/{shop['simple']['slug']}", 120),
                      ("/products/facets", 45), (f"/products/{shop['simple']['slug']}/related", 120)):
        assert (await client.get(PUBLIC + path)).headers["cache-control"].startswith(f"public, max-age={ttl},"), path

    private = await client.get(f"{PUBLIC}/products", headers=customer_headers)
    assert private.headers["cache-control"] == "private, no-cache" and "Authorization" in private.headers["vary"]
    assert (await client.get(f"{PUBLIC}/products", headers={**customer_headers, "If-None-Match": private.headers["etag"]})).status_code == 304

    for path in ("/api/v1/cart", "/api/v1/users/me", "/api/v1/notifications"):
        assert (await client.get(path, headers=customer_headers)).headers["cache-control"] == "no-store", path
    assert (await client.get("/api/v1/no-such-route")).headers["cache-control"] == "no-store"
    missing = await client.get(f"{PUBLIC}/products/no-such-product")
    assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store"


def test_every_admin_catalog_write_route_bumps_the_catalog() -> None:
    from app.modules.banners.router import admin_router as banners
    from app.modules.brands.router import admin_router as brands
    from app.modules.categories.router import admin_router as categories
    from app.modules.products.admin_router import router as products
    from app.shared.catalog_cache import catalog_write

    writes = 0
    for router in (categories, brands, products, banners):
        assert any(d.dependency is catalog_write for d in router.dependencies), router.prefix
        writes += sum(1 for r in router.routes if set(getattr(r, "methods", ())) & {"POST", "PUT", "PATCH", "DELETE"})
    assert writes >= 30


async def test_checkout_reads_delivery_fees_fresh(
    client: AsyncClient, customer_headers: dict, shop: dict, db_session: AsyncSession
) -> None:
    from sqlalchemy import update

    from app.modules.delivery.models import DeliveryMethod
    from order_helpers import ORDERS, address

    ok(await client.post("/api/v1/cart/items", json={"product_id": shop["simple"]["id"], "quantity": 1}, headers=customer_headers), 201)
    cart = ok(await client.get("/api/v1/cart", headers=customer_headers))
    quote = next(q for q in cart["delivery_quotes"] if q["code"] == "damascus_courier")
    await db_session.execute(
        update(DeliveryMethod).where(DeliveryMethod.code == "damascus_courier").values(fee="20000.00")
    )
    await db_session.commit()  # no bump: other workers would still show the old fee for up to 60 s
    again = ok(await client.get("/api/v1/cart", headers=customer_headers))
    assert next(q for q in again["delivery_quotes"] if q["code"] == "damascus_courier")["fee"] == quote["fee"]
    destination = await address(client, customer_headers)
    stale_totals = {row["currency"]: row["amount"] for row in quote["totals"]}
    placed = await client.post(
        ORDERS,
        json={
            "delivery_method_code": "damascus_courier",
            "address_id": destination["id"],
            "expected_totals": stale_totals,
        },
        headers=customer_headers,
    )
    assert placed.status_code == 409 and placed.json()["error"]["code"] == "PRICE_CHANGED"  # checkout: fresh fee


async def test_detail_with_its_long_ttl_is_still_fresh_after_admin_edits_and_orders(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict, count_queries: Any
) -> None:
    """The detail TTL (120 s) is never what keeps it fresh: version bumps are."""
    from app.shared.http_cache import TTL

    assert TTL["product"] == 120
    cream = shop["simple"]
    path = f"{PUBLIC}/products/{cream['slug']}"
    first = await client.get(path)
    assert first.headers["cache-control"].startswith("public, max-age=120,")
    hit, cached = await statements(client, count_queries, path)
    assert hit == 0 and cached["name"] == "Cream"  # served from the cache

    ok(await client.patch(f"{ADMIN}/products/{cream['id']}", json={"name": "Cream Deluxe", "sale_price": "7.00"},
                          headers=admin_headers))
    count, edited = await statements(client, count_queries, path)
    assert count > 0 and (edited["name"], edited["current_price"]) == ("Cream Deluxe", "7.00")  # immediately

    hit, _ = await statements(client, count_queries, path)
    assert hit == 0  # cached again
    await order_from_cart(client, customer_headers, [(cream["id"], None, 5)], method="pickup")  # the last 5 units
    count, sold_out = await statements(client, count_queries, path)
    assert count > 0 and (sold_out["in_stock"], sold_out["stock_status"]) == (False, "out")  # immediately
