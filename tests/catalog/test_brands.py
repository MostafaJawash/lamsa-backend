import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.products.models import Product
from app.shared.utils.arabic import normalize_arabic
from catalog_helpers import ADMIN, PUBLIC, code_of, create_brand, create_category, create_product, ok, png, published_product

pytestmark = pytest.mark.anyio


async def test_brand_slug_and_delete_rules(client: AsyncClient, admin_headers: dict[str, str]) -> None:
    first = await create_brand(client, admin_headers, "Bella")
    second = await create_brand(client, admin_headers, "bella")
    assert (first["slug"], second["slug"]) == ("bella", "bella-2")
    taken = await client.post(f"{ADMIN}/brands", json={"name": "X", "slug": "bella"}, headers=admin_headers)
    assert taken.status_code == 409 and code_of(taken) == "SLUG_TAKEN"

    category = await create_category(client, admin_headers, "C")
    await create_product(client, admin_headers, name="P", category_id=category["id"], brand_id=first["id"])
    blocked = await client.delete(f"{ADMIN}/brands/{first['id']}", headers=admin_headers)
    assert blocked.status_code == 409 and code_of(blocked) == "BRAND_HAS_PRODUCTS"
    assert (await client.delete(f"{ADMIN}/brands/{second['id']}", headers=admin_headers)).status_code == 204
    gone = await client.get(f"{ADMIN}/brands/{second['id']}", headers=admin_headers)
    assert gone.status_code == 404 and code_of(gone) == "BRAND_NOT_FOUND"


async def test_public_brands_visibility_and_category_filter(client: AsyncClient, admin_headers: dict[str, str]) -> None:
    makeup = await create_category(client, admin_headers, "Makeup X")
    hair = await create_category(client, admin_headers, "Hair X")
    luma = await create_brand(client, admin_headers, "Luma", sort_order=1)
    sidr = await create_brand(client, admin_headers, "Sidr", sort_order=2)
    hidden = await create_brand(client, admin_headers, "Hidden", is_active=False)
    await published_product(client, admin_headers, name="A", category_id=makeup["id"], brand_id=luma["id"])
    await published_product(client, admin_headers, name="B", category_id=hair["id"], brand_id=luma["id"])
    await published_product(client, admin_headers, name="C", category_id=hair["id"], brand_id=sidr["id"])
    await create_product(client, admin_headers, name="Draft", category_id=hair["id"], brand_id=sidr["id"])

    brands = ok(await client.get(f"{PUBLIC}/brands"))
    assert [(b["slug"], b["product_count"]) for b in brands] == [("luma", 2), ("sidr", 1)]
    in_makeup = ok(await client.get(f"{PUBLIC}/brands", params={"category": makeup["slug"]}))
    assert [(b["slug"], b["product_count"]) for b in in_makeup] == [("luma", 1)]

    assert ok(await client.get(f"{PUBLIC}/brands/sidr"))["product_count"] == 1
    response = await client.get(f"{PUBLIC}/brands/{hidden['slug']}")
    assert response.status_code == 404 and code_of(response) == "BRAND_NOT_FOUND"

    # Deactivating a brand hides its products everywhere.
    ok(await client.patch(f"{ADMIN}/brands/{luma['id']}", json={"is_active": False}, headers=admin_headers))
    assert ok(await client.get(f"{PUBLIC}/products"))["total"] == 1


async def test_admin_brand_list_and_logo(client: AsyncClient, admin_headers: dict[str, str], media_root) -> None:
    for name in ("Ward", "Nour", "Misk"):
        await create_brand(client, admin_headers, name, is_active=name != "Misk")
    page = ok(await client.get(f"{ADMIN}/brands", params={"is_active": True, "page_size": 1}, headers=admin_headers))
    assert page["total"] == 2 and len(page["items"]) == 1
    assert ok(await client.get(f"{ADMIN}/brands", params={"search": "nou"}, headers=admin_headers))["items"][0]["name"] == "Nour"

    brand_id = page["items"][0]["id"]
    logo = ok(await client.put(f"{ADMIN}/brands/{brand_id}/logo", files={"file": ("l.png", png(), "image/png")}, headers=admin_headers))
    assert logo["logo"]["thumb"].endswith("_thumb.webp")
    assert ok(await client.delete(f"{ADMIN}/brands/{brand_id}/logo", headers=admin_headers))["logo"] is None
    assert list((media_root / "brands" / str(brand_id)).iterdir()) == []


async def test_brand_rename_updates_search_text(
    client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession
) -> None:
    category = await create_category(client, admin_headers, "C")
    brand = await create_brand(client, admin_headers, "Old Name")
    product = await create_product(client, admin_headers, name="P", category_id=category["id"], brand_id=brand["id"])
    ok(await client.patch(f"{ADMIN}/brands/{brand['id']}", json={"name": "لُمى"}, headers=admin_headers))
    text = await db_session.scalar(
        select(Product.search_text).where(Product.id == product["id"]).execution_options(populate_existing=True)
    )
    assert normalize_arabic("لُمى") == "لمي"
    assert "لمي" in (text or "") and "old name" not in (text or "")
