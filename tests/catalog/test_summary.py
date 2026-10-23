"""price_from / in_stock / is_on_sale are recalculated after every trigger listed in the spec."""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.products.models import Product
from catalog_helpers import ADMIN, add_variant_option, create_category, create_product, ok, publish, upload_images, variant
from scripts.recalculate_products import recalculate_all

pytestmark = pytest.mark.anyio


async def summary(client: AsyncClient, headers: dict[str, str], product_id: int) -> tuple[str, bool, bool]:
    data = ok(await client.get(f"{ADMIN}/products/{product_id}", headers=headers))
    return data["price_from"], data["in_stock"], data["is_on_sale"]


@pytest.fixture
async def cid(client: AsyncClient, admin_headers: dict[str, str]) -> int:
    category = await create_category(client, admin_headers, "Summary")
    await add_variant_option(client, admin_headers, category["id"], key="color", label="اللون", type="color")
    return int(category["id"])


async def test_simple_product_triggers(client: AsyncClient, admin_headers: dict[str, str], cid: int) -> None:
    p = await create_product(client, admin_headers, name="S", category_id=cid, price="10", stock=0)
    assert await summary(client, admin_headers, p["id"]) == ("10.00", False, False)
    url = f"{ADMIN}/products/{p['id']}"
    ok(await client.patch(url, json={"sale_price": "8.00"}, headers=admin_headers))
    assert await summary(client, admin_headers, p["id"]) == ("8.00", False, True)
    ok(await client.patch(url, json={"stock": 2}, headers=admin_headers))
    assert await summary(client, admin_headers, p["id"]) == ("8.00", True, True)
    ok(await client.patch(url, json={"sale_price": None, "price": "12"}, headers=admin_headers))
    assert await summary(client, admin_headers, p["id"]) == ("12.00", True, False)
    ok(await client.post(f"{url}/stock-adjust", json={"delta": -2}, headers=admin_headers))
    assert await summary(client, admin_headers, p["id"]) == ("12.00", False, False)


async def test_variable_product_triggers(client: AsyncClient, admin_headers: dict[str, str], cid: int) -> None:
    p = await create_product(client, admin_headers, type="variable", name="V", category_id=cid)
    assert await summary(client, admin_headers, p["id"]) == ("0.00", False, False)  # no active variants
    url = f"{ADMIN}/products/{p['id']}"

    created = ok(await client.post(f"{url}/variants", json=variant("أحمر", price="20", stock=0), headers=admin_headers), 201)
    red = created["variants"][0]
    assert await summary(client, admin_headers, p["id"]) == ("20.00", False, False)  # none in stock: min of active

    created = ok(await client.post(f"{url}/variants", json=variant("أزرق", "#0000FF", price="30", stock=4), headers=admin_headers), 201)
    blue = next(v for v in created["variants"] if v["id"] != red["id"])
    assert await summary(client, admin_headers, p["id"]) == ("30.00", True, False)  # in-stock preferred

    ok(await client.patch(f"{url}/variants/{blue['id']}", json={"sale_price": "25"}, headers=admin_headers))
    assert await summary(client, admin_headers, p["id"]) == ("25.00", True, True)

    ok(await client.post(f"{url}/variants/{red['id']}/stock-adjust", json={"delta": 3}, headers=admin_headers))
    assert await summary(client, admin_headers, p["id"]) == ("20.00", True, True)

    ok(await client.patch(f"{url}/variants/{red['id']}", json={"is_active": False}, headers=admin_headers))
    assert await summary(client, admin_headers, p["id"]) == ("25.00", True, True)

    ok(await client.delete(f"{url}/variants/{blue['id']}", headers=admin_headers))
    assert await summary(client, admin_headers, p["id"]) == ("0.00", False, False)  # only an inactive one left


async def test_bundle_reacts_to_components(client: AsyncClient, admin_headers: dict[str, str], cid: int) -> None:
    simple = await create_product(client, admin_headers, name="Cream", category_id=cid, price="7", stock=5)
    var = await create_product(client, admin_headers, type="variable", name="Lip", category_id=cid,
                               variants=[variant("أحمر", price="9", stock=3)])
    red_id = var["variants"][0]["id"]
    for p in (simple, var):
        ok(await upload_images(client, admin_headers, p["id"]), 201)
        await publish(client, admin_headers, p["id"])
    bundle = await create_product(client, admin_headers, type="bundle", name="Box", category_id=cid, price="15", sale_price="12",
                                  bundle_items=[{"product_id": simple["id"], "quantity": 2}, {"product_id": var["id"], "variant_id": red_id}])
    bid = bundle["id"]
    detail = ok(await client.get(f"{ADMIN}/products/{bid}", headers=admin_headers))
    assert detail["bundle_stock"] == 2  # min(5 // 2, 3 // 1)
    assert await summary(client, admin_headers, bid) == ("12.00", True, True)

    # Component stock -> bundle.
    ok(await client.post(f"{ADMIN}/products/{simple['id']}/stock-adjust", json={"delta": -4}, headers=admin_headers))
    assert await summary(client, admin_headers, bid) == ("12.00", False, True)  # 1 // 2 = 0
    ok(await client.post(f"{ADMIN}/products/{simple['id']}/stock-adjust", json={"delta": 9}, headers=admin_headers))
    assert await summary(client, admin_headers, bid) == ("12.00", True, True)

    # Component variant stock and activation -> bundle.
    ok(await client.post(f"{ADMIN}/products/{var['id']}/variants/{red_id}/stock-adjust", json={"delta": -3}, headers=admin_headers))
    assert (await summary(client, admin_headers, bid))[1] is False
    ok(await client.post(f"{ADMIN}/products/{var['id']}/variants/{red_id}/stock-adjust", json={"delta": 3}, headers=admin_headers))
    assert (await summary(client, admin_headers, bid))[1] is True
    ok(await client.patch(f"{ADMIN}/products/{var['id']}/variants/{red_id}", json={"is_active": False}, headers=admin_headers))
    assert (await summary(client, admin_headers, bid))[1] is False
    ok(await client.patch(f"{ADMIN}/products/{var['id']}/variants/{red_id}", json={"is_active": True}, headers=admin_headers))
    assert (await summary(client, admin_headers, bid))[1] is True

    # Component product activation -> bundle.
    ok(await client.patch(f"{ADMIN}/products/{simple['id']}", json={"is_active": False}, headers=admin_headers))
    assert (await summary(client, admin_headers, bid))[1] is False
    ok(await client.patch(f"{ADMIN}/products/{simple['id']}", json={"is_active": True}, headers=admin_headers))
    assert (await summary(client, admin_headers, bid))[1] is True

    # Component stock via PATCH, and bundle items replace.
    ok(await client.patch(f"{ADMIN}/products/{simple['id']}", json={"stock": 0}, headers=admin_headers))
    assert (await summary(client, admin_headers, bid))[1] is False
    ok(await client.put(f"{ADMIN}/products/{bid}/bundle-items", json={"items": [{"product_id": var["id"], "variant_id": red_id}]}, headers=admin_headers))
    assert (await summary(client, admin_headers, bid))[1] is True
    ok(await client.patch(f"{ADMIN}/products/{bid}", json={"sale_price": None}, headers=admin_headers))
    assert await summary(client, admin_headers, bid) == ("15.00", True, False)


async def test_recalculate_script_rebuilds_everything(
    client: AsyncClient, admin_headers: dict[str, str], cid: int, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    product = await create_product(client, admin_headers, name="زيت أرغان", category_id=cid, price="9", sale_price="7", stock=3)
    await db_session.execute(
        update(Product).where(Product.id == product["id"]).values(price_from=0, in_stock=False, is_on_sale=False, search_text="")
    )
    await db_session.commit()

    class SessionFactory:
        def __call__(self) -> Any:
            return _Borrowed(db_session)

    monkeypatch.setattr("scripts.recalculate_products.SessionLocal", SessionFactory())
    assert await recalculate_all() >= 1
    row = (await db_session.execute(
        select(Product.price_from, Product.in_stock, Product.is_on_sale, Product.search_text)
        .where(Product.id == product["id"]).execution_options(populate_existing=True)
    )).one()
    assert (str(row.price_from), row.in_stock, row.is_on_sale) == ("7.00", True, True)
    assert "زيت ارغان" in row.search_text


async def test_orm_insert_without_search_text_stores_empty_string(db_session: AsyncSession, cid: int) -> None:
    product = Product(name="Raw", slug="raw-product", category_id=cid, type="simple", price=1, stock=1)
    db_session.add(product)
    await db_session.flush()
    stored = await db_session.scalar(
        select(Product.search_text).where(Product.id == product.id).execution_options(populate_existing=True)
    )
    assert stored == ""


class _Borrowed:
    """Use the test session as `async with SessionLocal() as session` without closing it."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def __aenter__(self) -> AsyncSession:
        return self.session

    async def __aexit__(self, *exc: object) -> None:
        return None
