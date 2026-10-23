"""End-to-end smoke test of the main catalog flow (kept small; detailed tests live elsewhere)."""

import pytest
from httpx import AsyncClient

from catalog_helpers import (
    ADMIN,
    PUBLIC,
    create_brand,
    create_product,
    ok,
    publish,
    upload_images,
    variant,
)

pytestmark = pytest.mark.anyio


async def test_create_publish_and_browse(
    client: AsyncClient, admin_headers: dict[str, str], base_categories: dict[str, int]
) -> None:
    brand = await create_brand(client, admin_headers, "لُمى", slug="luma")
    product = await create_product(
        client, admin_headers, type="variable", name="أحمر شفاه مخملي", category_id=base_categories["makeup"],
        brand_id=brand["id"], attributes={"finish": "مطفي", "ingredients": "زبدة الشيا"},
        variants=[variant("وردي ترابي", "#b5656b", stock=12), variant("أحمر كلاسيكي", "#B3202C", stock=3, sale_price="7.95")],
    )
    assert product["is_active"] is False
    assert product["publishable"] == {"ok": False, "reasons": ["NO_IMAGES"]}
    assert [v["sku"] for v in product["variants"]] == [f"LM-{product['id']}-1", f"LM-{product['id']}-2"]
    assert product["variants"][0]["options"]["color_hex"] == "#B5656B"

    ok(await upload_images(client, admin_headers, product["id"]), 201)
    published = await publish(client, admin_headers, product["id"])
    assert published["is_active"] is True

    page = ok(await client.get(f"{PUBLIC}/products"))
    assert page["total"] == 1
    card = page["items"][0]
    assert card["price"] == "7.95" and card["compare_at_price"] == "10.00" and card["discount_percent"] == 21
    assert card["swatches"] == [{"name": "وردي ترابي", "hex": "#B5656B"}, {"name": "أحمر كلاسيكي", "hex": "#B3202C"}]

    detail = ok(await client.get(f"{PUBLIC}/products/{card['slug']}"))
    assert [a["key"] for a in detail["attributes"]] == ["ingredients", "finish"]
    assert {v["stock_status"] for v in detail["variants"]} == {"in_stock", "low"}

    tree = ok(await client.get(f"{PUBLIC}/categories"))
    makeup = next(c for c in tree if c["slug"] == "makeup")
    assert makeup["product_count"] == 1
    assert ok(await client.get(f"{ADMIN}/products", headers=admin_headers))["total"] == 1
