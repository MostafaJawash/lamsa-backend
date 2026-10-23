"""Two parallel adds of the same new item, on two real connections with real commits (the
per-user row lock only shows its effect across separate transactions). Uses lamsa_test and wipes
the tables afterwards."""

import asyncio
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
from app.modules.cart.models import CartItem
from app.modules.users.models import User
from cart_fixtures import CART, build_shop
from conftest import bearer, register

pytestmark = pytest.mark.anyio

CLEANUP = [
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
        async with maker() as session:  # every request gets its own connection
            yield session

    app.dependency_overrides[get_db] = real_session
    try:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            admin = await register(client, 91, name="Admin")
            async with maker() as session:
                await session.execute(update(User).where(User.id == admin["user"]["id"]).values(role="admin"))
                await session.commit()
                shop = await build_shop(client, bearer(admin["access_token"]), session)
            customer = await register(client, 92, name="Customer")
            yield {"client": client, "maker": maker, "shop": shop, "customer": customer}
    finally:
        app.dependency_overrides.pop(get_db, None)
        async with engine.begin() as conn:
            for statement in CLEANUP:
                await conn.execute(text(statement))
        await engine.dispose()


async def test_parallel_adds_of_the_same_new_item(committed: dict[str, Any]) -> None:
    client, shop = committed["client"], committed["shop"]
    headers = bearer(committed["customer"]["access_token"])
    body = {"product_id": shop["lip"]["id"], "variant_id": shop["red"]["id"], "quantity": 1}

    first, second = await asyncio.gather(
        client.post(f"{CART}/items", json=body, headers=headers),
        client.post(f"{CART}/items", json=body, headers=headers),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 201], (first.text, second.text)

    async with committed["maker"]() as session:
        rows = (await session.execute(
            select(CartItem.quantity).where(CartItem.user_id == committed["customer"]["user"]["id"])
        )).scalars().all()
        assert rows == [2]  # one line, summed
        assert await session.scalar(select(func.count()).select_from(CartItem)) == 1


async def test_parallel_merges_respect_the_line_limit(committed: dict[str, Any]) -> None:
    client, shop = committed["client"], committed["shop"]
    headers = bearer(committed["customer"]["access_token"])
    items = [{"product_id": shop["simple"]["id"]}, {"product_id": shop["lip"]["id"], "variant_id": shop["red"]["id"]}]
    responses = await asyncio.gather(*[client.post(f"{CART}/merge", json={"items": items}, headers=headers) for _ in range(3)])
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    final = (await client.get(CART, headers=headers)).json()
    assert final["lines_count"] == 2
    assert sorted(l["quantity"] for l in final["lines"]) == [3, 3]
