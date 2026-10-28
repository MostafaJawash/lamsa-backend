"""Arabic search. InnoDB FULLTEXT only sees COMMITTED rows, so these tests use a fixture that
really commits to the test database (lamsa_test) and wipes the catalog/user tables afterwards."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db.session import CONNECT_ARGS, get_db
from app.main import app
from app.modules.users.models import User
from app.shared.utils.search import build_boolean_query, search_tokens
from catalog_helpers import (
    ADMIN,
    PUBLIC,
    add_attribute,
    add_variant_option,
    create_brand,
    create_category,
    ok,
    published_product,
    variant,
)
from conftest import bearer, register

pytestmark = pytest.mark.anyio

CLEANUP = [
    "DELETE FROM bundle_items",
    "DELETE FROM products",
    "DELETE FROM categories WHERE parent_id IS NOT NULL",
    "DELETE FROM categories",
    "DELETE FROM brands",
    "DELETE FROM users",
    "DELETE FROM login_attempts",
    "DELETE FROM store_settings WHERE `key` = 'catalog_hide_out_of_stock'",
]


@pytest.fixture
async def committed(migrated_db: None) -> AsyncIterator[tuple[AsyncClient, dict[str, str]]]:
    assert settings.DB_NAME.endswith("_test")
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool, connect_args=CONNECT_ARGS)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def real_session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db] = real_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
            data = await register(client, 90, name="Search Admin")
            async with maker() as session:
                await session.execute(update(User).where(User.id == data["user"]["id"]).values(role="admin"))
                await session.commit()
            yield client, bearer(data["access_token"])
    finally:
        app.dependency_overrides.pop(get_db, None)
        async with engine.begin() as conn:
            for statement in CLEANUP:
                await conn.execute(text(statement))
        await engine.dispose()


@pytest.fixture
async def products(committed: tuple[AsyncClient, dict[str, str]]) -> dict[str, Any]:
    client, headers = committed
    care = await create_category(client, headers, "عناية بالبشرة", slug="skin-care")
    hair = await create_category(client, headers, "شعر", slug="hair-s")
    await add_attribute(client, headers, care["id"], key="ingredients", label="المكونات", type="text")
    await add_variant_option(client, headers, care["id"], key="color", label="اللون", type="color")
    luma = await create_brand(client, headers, "لُمى", slug="luma-s")

    made = {
        "argan": await published_product(client, headers, name="زيت أرغان للشعر", category_id=hair["id"],
                                          description="زيت أرغان طبيعي يغذي الشعر، زيت أرغان مغربي"),
        "cream": await published_product(client, headers, name="كريم عناية بالبشرة", category_id=care["id"],
                                          attributes={"ingredients": "زبدة الشيا"}),
        "layla": await published_product(client, headers, name="عطر ليلى", category_id=care["id"], brand_id=luma["id"]),
        "lotion": await published_product(client, headers, name="مُرطّب الجسم", category_id=care["id"]),
        "musk": await published_product(client, headers, name="عطــر المسك", category_id=care["id"], description="حجم ٥٠ مل"),
        "outing": await published_product(client, headers, name="إطلالة مسائية", category_id=care["id"]),
        "mention": await published_product(client, headers, name="شامبو", category_id=hair["id"], description="فيه ارغان"),
    }
    var = await published_product(client, headers, type="variable", name="Velvet Lipstick", category_id=care["id"],
                                  variants=[variant("أحمر كلاسيكي", "#B3202C")])
    made["lipstick"] = var
    return made


async def search(client: AsyncClient, q: str, **params: Any) -> list[str]:
    page = ok(await client.get(f"{PUBLIC}/products", params={"q": q, **params}))
    return [i["name"] for i in page["items"]]


# ----------------------------------------------------------------- letter folding


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("ارغان", "زيت أرغان للشعر"),  # hamza on alef
        ("أرغان", "زيت أرغان للشعر"),
        ("عنايه", "كريم عناية بالبشرة"),  # taa marbuta
        ("ليلي", "عطر ليلى"),  # alef maksura
        ("مرطب", "مُرطّب الجسم"),  # tashkeel in the product
        ("مُرَطِّب", "مُرطّب الجسم"),  # tashkeel in the query
        ("عطر المسك", "عطــر المسك"),  # tatweel
        ("50", "عطــر المسك"),  # Arabic-Indic digits in the product
        ("٥٠", "عطــر المسك"),
        ("اطلالة", "إطلالة مسائية"),  # hamza under alef
    ],
)
async def test_letter_folding(committed: Any, products: dict[str, Any], query: str, expected: str) -> None:
    client, _ = committed
    assert expected in await search(client, query)


# --------------------------------------------------------------------- matching


async def test_matches_brand_category_attribute_and_option(committed: Any, products: dict[str, Any]) -> None:
    client, _ = committed
    assert await search(client, "لمى") == ["عطر ليلى"]  # brand name (stored with tashkeel)
    assert set(await search(client, "شعر")) >= {"زيت أرغان للشعر", "شامبو"}  # category name
    assert await search(client, "الشيا") == ["كريم عناية بالبشرة"]  # text attribute value
    assert await search(client, "كلاسيكي") == ["Velvet Lipstick"]  # variant option value
    assert await search(client, "lipstick") == ["Velvet Lipstick"]


async def test_every_token_is_required(committed: Any, products: dict[str, Any]) -> None:
    client, _ = committed
    assert await search(client, "زيت للشعر") == ["زيت أرغان للشعر"]
    assert await search(client, "زيت المسك") == []


async def test_operators_are_stripped_and_short_tokens_dropped(committed: Any, products: dict[str, Any]) -> None:
    client, _ = committed
    assert await search(client, '+زيت -ارغان* "(للشعر)" ~@') == ["زيت أرغان للشعر"]
    assert await search(client, "ز ارغان") == await search(client, "ارغان")
    for nothing in ("a", "+-*", '"', "  "):
        page = ok(await client.get(f"{PUBLIC}/products", params={"q": nothing}))
        if nothing.strip():
            assert page == {"items": [], "page": 1, "page_size": 20, "total": 0}, nothing


async def test_relevance_is_default_with_q(committed: Any, products: dict[str, Any]) -> None:
    client, _ = committed
    # "argan" is in the name and twice in the description; "mention" only once in its description.
    assert await search(client, "ارغان") == ["زيت أرغان للشعر", "شامبو"]
    assert await search(client, "ارغان", sort="newest") == ["شامبو", "زيت أرغان للشعر"]
    facets = ok(await client.get(f"{PUBLIC}/products/facets", params={"q": "ارغان"}))
    assert facets["prices"][0]["min"] is not None


async def test_admin_search_by_text_and_sku(committed: Any, products: dict[str, Any]) -> None:
    client, headers = committed
    names = [i["name"] for i in ok(await client.get(f"{ADMIN}/products", params={"q": "كريم"}, headers=headers))["items"]]
    assert names == ["كريم عناية بالبشرة"]
    # The category name is part of every product's search text, so it matches the whole category.
    in_category = ok(await client.get(f"{ADMIN}/products", params={"q": "عنايه"}, headers=headers))
    assert in_category["total"] == 6
    sku = products["lipstick"]["variants"][0]["sku"]
    assert ok(await client.get(f"{ADMIN}/products", params={"q": sku.lower()}, headers=headers))["items"][0]["name"] == "Velvet Lipstick"


def test_query_builder_units() -> None:
    assert search_tokens("  أَرغان   زيت  ") == ["ارغان", "زيت"]
    assert build_boolean_query('زيت+ارغان "x" (a) ب') == '+"زيت" +"ارغان"'
    assert build_boolean_query("a b c") is None
    assert build_boolean_query("one two three four five six seven") == '+"one" +"two" +"three" +"four" +"five" +"six"'
    assert build_boolean_query("ok ok") == '+"ok"'


async def test_search_leaves_out_of_stock_products_out_unless_the_setting_is_off(
    committed: Any, products: dict[str, Any]
) -> None:
    client, headers = committed
    care = products["cream"]["category"]["id"]
    await published_product(client, headers, name="عطر الورد النادر", category_id=care, stock=0)
    assert await search(client, "النادر") == []  # default: hidden while browsing
    ok(await client.patch(f"{ADMIN}/settings", json={"catalog_hide_out_of_stock": False}, headers=headers))
    assert await search(client, "النادر") == ["عطر الورد النادر"]
