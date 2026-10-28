"""Checkout races on real connections with real commits (lamsa_test; wiped afterwards)."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db.session import CONNECT_ARGS, get_db
from app.main import app
from app.modules.orders.models import Order
from app.modules.products.models import Product, ProductVariant
from app.modules.users.models import User
from cart_fixtures import add, build_shop
from conftest import bearer, register
from order_helpers import address, expected_total, place

pytestmark = pytest.mark.anyio

CLEANUP = [
    "DELETE FROM orders",
    "DELETE FROM cart_items",
    "DELETE FROM favorites",
    "DELETE FROM bundle_items",
    "DELETE FROM products",
    "DELETE FROM categories WHERE parent_id IS NOT NULL",
    "DELETE FROM categories",
    "DELETE FROM brands",
    "DELETE FROM delivery_methods",
    "DELETE FROM users",
    "DELETE FROM login_attempts",
]


@pytest.fixture
async def committed(migrated_db: None) -> AsyncIterator[dict[str, Any]]:
    assert settings.DB_NAME.endswith("_test")
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool, connect_args=CONNECT_ARGS)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def real_session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db] = real_session
    try:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            admin = await register(client, 81, name="Admin")
            async with maker() as session:
                await session.execute(update(User).where(User.id == admin["user"]["id"]).values(role="admin"))
                await session.commit()
                shop = await build_shop(client, bearer(admin["access_token"]), session)
            users = [bearer((await register(client, n, name=f"Buyer {n}"))["access_token"]) for n in (82, 83)]
            for user in users:
                await address(client, user)
            yield {"client": client, "maker": maker, "shop": shop, "users": users}
    finally:
        app.dependency_overrides.pop(get_db, None)
        async with engine.begin() as conn:
            for statement in CLEANUP:
                await conn.execute(text(statement))
        await engine.dispose()


async def set_stock(maker: Any, product_id: int, stock: int) -> None:
    async with maker() as session:
        await session.execute(update(Product).where(Product.id == product_id).values(stock=stock))
        await session.commit()


async def test_two_users_buy_the_last_unit(committed: dict[str, Any]) -> None:
    client, shop, (alice, bob) = committed["client"], committed["shop"], committed["users"]
    for user in (alice, bob):
        assert (await add(client, user, shop["simple"]["id"])).status_code == 201
    await set_stock(committed["maker"], shop["simple"]["id"], 1)
    totals = [await expected_total(client, user, "pickup") for user in (alice, bob)]

    results = await asyncio.gather(*[place(client, user, method="pickup", total=total) for user, total in zip((alice, bob), totals)])
    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 409], [r.text for r in results]
    loser = next(r for r in results if r.status_code == 409)
    # The loser read the CURRENT stock under the row lock: its cart line is now out of stock.
    assert loser.json()["error"]["code"] == "CART_HAS_ISSUES"
    assert loser.json()["error"]["details"]["cart"]["lines"][0]["issue"]["code"] == "OUT_OF_STOCK"
    async with committed["maker"]() as session:
        assert await session.scalar(select(Product.stock).where(Product.id == shop["simple"]["id"])) == 0
        assert await session.scalar(select(func.count()).select_from(Order)) == 1


async def test_same_user_double_submit(committed: dict[str, Any]) -> None:
    client, shop, (alice, _) = committed["client"], committed["shop"], committed["users"]
    assert (await add(client, alice, shop["simple"]["id"])).status_code == 201
    total = await expected_total(client, alice, "pickup")

    without_key = await asyncio.gather(place(client, alice, method="pickup", total=total), place(client, alice, method="pickup", total=total))
    assert sorted(r.status_code for r in without_key) == [201, 422]
    assert next(r for r in without_key if r.status_code == 422).json()["error"]["code"] == "CART_EMPTY"

    assert (await add(client, alice, shop["simple"]["id"])).status_code == 201
    key = str(uuid.uuid4())
    with_key = await asyncio.gather(place(client, alice, method="pickup", total=total, key=key),
                                    place(client, alice, method="pickup", total=total, key=key))
    assert sorted(r.status_code for r in with_key) == [200, 201], [r.text for r in with_key]
    assert with_key[0].json()["id"] == with_key[1].json()["id"]
    async with committed["maker"]() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 2
        assert await session.scalar(select(Product.stock).where(Product.id == shop["simple"]["id"])) == 3  # 5 - 1 - 1


async def test_overlapping_products_in_different_cart_order_do_not_deadlock(committed: dict[str, Any]) -> None:
    client, shop, (alice, bob) = committed["client"], committed["shop"], committed["users"]
    await set_stock(committed["maker"], shop["simple"]["id"], 50)
    async with committed["maker"]() as session:
        await session.execute(update(ProductVariant).where(ProductVariant.id == shop["red"]["id"]).values(stock=50))
        await session.commit()
    # Alice adds A then B; Bob adds B then A (plus a bundle that locks both again).
    for product_id, variant_id in ((shop["simple"]["id"], None), (shop["lip"]["id"], shop["red"]["id"]), (shop["bundle"]["id"], None)):
        assert (await add(client, alice, product_id, variant_id)).status_code == 201
    for product_id, variant_id in ((shop["bundle"]["id"], None), (shop["lip"]["id"], shop["red"]["id"]), (shop["simple"]["id"], None)):
        assert (await add(client, bob, product_id, variant_id)).status_code == 201
    totals = [await expected_total(client, user, "pickup") for user in (alice, bob)]
    for _ in range(3):
        results = await asyncio.gather(*[place(client, user, method="pickup", total=t) for user, t in zip((alice, bob), totals)])
        assert all(r.status_code == 201 for r in results), [r.text for r in results]
        for user in (alice, bob):
            for product_id, variant_id in ((shop["simple"]["id"], None), (shop["bundle"]["id"], None)):
                assert (await add(client, user, product_id, variant_id)).status_code == 201
        totals = [await expected_total(client, user, "pickup") for user in (alice, bob)]
    async with committed["maker"]() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 6
