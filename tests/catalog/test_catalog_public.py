from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.settings.models import StoreSetting
from catalog_helpers import (
    ADMIN,
    PUBLIC,
    add_attribute,
    add_variant_option,
    code_of,
    create_brand,
    create_category,
    create_product,
    ok,
    publish,
    published_product,
    upload_images,
    variant,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def catalog(client: AsyncClient, admin_headers: dict[str, str]) -> dict[str, Any]:
    """Two-level tree with attributes of every filterable type, two brands, five products."""
    top = await create_category(client, admin_headers, "Beauty", slug="beauty")
    child = await create_category(client, admin_headers, "Lips", slug="lips", parent_id=top["id"])
    other = await create_category(client, admin_headers, "Hair", slug="hair-x")
    await add_attribute(client, admin_headers, top["id"], key="finish", label="اللمسة", type="select", options=["مطفي", "لامع", "ساتان"], is_filterable=True)
    await add_attribute(client, admin_headers, top["id"], key="skin", label="البشرة", type="multiselect", options=["جافة", "دهنية", "مختلطة"], is_filterable=True)
    await add_attribute(client, admin_headers, top["id"], key="spf", label="SPF", type="number", is_filterable=True)
    await add_attribute(client, admin_headers, top["id"], key="vegan", label="نباتي", type="boolean", is_filterable=True)
    await add_attribute(client, admin_headers, top["id"], key="hidden_note", label="x", type="text")
    await add_variant_option(client, admin_headers, top["id"], key="color", label="اللون", type="color")
    luma = await create_brand(client, admin_headers, "Luma", slug="luma")
    bella = await create_brand(client, admin_headers, "Bella", slug="bella")

    a = await published_product(client, admin_headers, name="Matte Lip", category_id=child["id"], brand_id=luma["id"],
                                price="10", stock=10, is_new=True,
                                attributes={"finish": "مطفي", "skin": ["جافة", "دهنية"], "spf": 15, "vegan": True})
    b = await published_product(client, admin_headers, name="Gloss", category_id=child["id"], brand_id=bella["id"],
                                price="20", sale_price="15", stock=0,
                                attributes={"finish": "لامع", "skin": ["مختلطة"], "spf": 30, "vegan": False})
    c = await create_product(client, admin_headers, type="variable", name="Shades", category_id=top["id"], brand_id=luma["id"],
                             attributes={"finish": "ساتان", "skin": ["جافة"], "spf": 50},
                             variants=[variant("أحمر", "#B3202C", price="12", stock=5, sale_price="9"),
                                       variant("وردي", "#FFC0CB", price="12", stock=0)])
    ok(await upload_images(client, admin_headers, c["id"]), 201)
    c = await publish(client, admin_headers, c["id"])
    d = await published_product(client, admin_headers, name="Shampoo", category_id=other["id"], price="5", stock=2)
    draft = await create_product(client, admin_headers, name="Draft", category_id=child["id"], brand_id=luma["id"])
    return {"top": top, "child": child, "other": other, "luma": luma, "bella": bella, "a": a, "b": b, "c": c, "d": d, "draft": draft}


async def names(client: AsyncClient, params: Any = None) -> list[str]:
    page = ok(await client.get(f"{PUBLIC}/products", params=params))
    return sorted(i["name"] for i in page["items"])


# ------------------------------------------------------------------------- visibility


async def test_visibility_rules(client: AsyncClient, admin_headers: dict[str, str], catalog: dict[str, Any]) -> None:
    assert await names(client) == ["Gloss", "Matte Lip", "Shades", "Shampoo"]
    draft = await client.get(f"{PUBLIC}/products/{catalog['draft']['slug']}")
    assert draft.status_code == 404 and code_of(draft) == "PRODUCT_NOT_FOUND"

    ok(await client.patch(f"{ADMIN}/brands/{catalog['bella']['id']}", json={"is_active": False}, headers=admin_headers))
    assert "Gloss" not in await names(client)
    assert (await client.get(f"{PUBLIC}/products/{catalog['b']['slug']}")).status_code == 404

    ok(await client.patch(f"{ADMIN}/categories/{catalog['child']['id']}", json={"is_active": False}, headers=admin_headers))
    assert await names(client) == ["Shades", "Shampoo"]
    ok(await client.patch(f"{ADMIN}/categories/{catalog['child']['id']}", json={"is_active": True}, headers=admin_headers))
    ok(await client.patch(f"{ADMIN}/categories/{catalog['top']['id']}", json={"is_active": False}, headers=admin_headers))
    assert await names(client) == ["Shampoo"]  # inactive parent hides the child's products too
    tree = ok(await client.get(f"{PUBLIC}/categories"))
    assert [c["slug"] for c in tree] == ["hair-x"]


# ---------------------------------------------------------------------------- filters


async def test_filters(client: AsyncClient, catalog: dict[str, Any]) -> None:
    assert await names(client, {"category": "beauty"}) == ["Gloss", "Matte Lip", "Shades"]  # includes children
    assert await names(client, {"category": "lips"}) == ["Gloss", "Matte Lip"]
    assert await names(client, [("brand", "luma"), ("brand", "bella")]) == ["Gloss", "Matte Lip", "Shades"]
    assert await names(client, {"brand": "bella"}) == ["Gloss"]
    assert await names(client, {"min_price": "9", "max_price": "10"}) == ["Matte Lip", "Shades"]
    assert await names(client, {"in_stock": "true"}) == ["Matte Lip", "Shades", "Shampoo"]
    assert await names(client, {"on_sale": "true"}) == ["Gloss", "Shades"]
    assert await names(client, {"is_new": "true"}) == ["Matte Lip"]

    beauty = {"category": "beauty"}
    assert await names(client, [*beauty.items(), ("attr.finish", "مطفي"), ("attr.finish", "لامع")]) == ["Gloss", "Matte Lip"]
    assert await names(client, [*beauty.items(), ("attr.finish", "مطفي"), ("attr.vegan", "false")]) == []
    assert await names(client, [*beauty.items(), ("attr.skin", "جافة")]) == ["Matte Lip", "Shades"]
    assert await names(client, [*beauty.items(), ("attr.skin", "مختلطة"), ("attr.skin", "دهنية")]) == ["Gloss", "Matte Lip"]
    assert await names(client, [*beauty.items(), ("attr.spf.min", "20"), ("attr.spf.max", "40")]) == ["Gloss"]
    assert await names(client, [*beauty.items(), ("attr.spf", "50")]) == ["Shades"]
    assert await names(client, [*beauty.items(), ("attr.vegan", "true")]) == ["Matte Lip"]
    assert await names(client, [*beauty.items(), ("opt.color", "أحمر")]) == ["Shades"]
    assert await names(client, [*beauty.items(), ("opt.color", "أزرق")]) == []
    # Without a category, keys from any category's filterable definitions are accepted.
    assert await names(client, {"attr.finish": "ساتان"}) == ["Shades"]


@pytest.mark.parametrize(
    "params",
    [
        {"category": "beauty", "attr.unknown": "x"},
        {"category": "beauty", "attr.hidden_note": "x"},  # exists but not filterable
        {"category": "beauty", "attr.finish": "ذهبي"},
        {"category": "beauty", "attr.vegan": "yes"},
        {"category": "beauty", "attr.spf.min": "abc"},
        {"category": "beauty", "attr.finish.min": "1"},
        {"category": "beauty", "attr.spf.min": "40", "attr.spf.max": "10"},
        {"category": "beauty", "opt.size": "50"},
        {"min_price": "10", "max_price": "5"},
        {"category": "beauty", "attr.finish') OR 1=1 -- ": "x"},
        {"category": "beauty", 'attr.finish"]': "x"},
        {"category": "beauty", "attr.$.x": "x"},
    ],
)
async def test_invalid_filters(client: AsyncClient, catalog: dict[str, Any], params: dict[str, str]) -> None:
    response = await client.get(f"{PUBLIC}/products", params=params)
    assert response.status_code == 422 and code_of(response) == "INVALID_FILTER", params
    assert "param" in response.json()["error"]["details"]


async def test_injection_in_values_is_harmless(client: AsyncClient, catalog: dict[str, Any]) -> None:
    for value in ["' OR 1=1 -- ", '") OR ("1"="1', "%", "\\"]:
        response = await client.get(f"{PUBLIC}/products", params={"category": "beauty", "opt.color": value})
        assert ok(response)["total"] == 0, value
        response = await client.get(f"{PUBLIC}/products", params={"q": value})
        assert response.status_code == 200, value
    assert ok(await client.get(f"{PUBLIC}/products"))["total"] == 4  # nothing was harmed


async def test_unknown_category_is_404(client: AsyncClient, catalog: dict[str, Any]) -> None:
    response = await client.get(f"{PUBLIC}/products", params={"category": "nope"})
    assert response.status_code == 404 and code_of(response) == "CATEGORY_NOT_FOUND"


async def test_sorting_and_pagination(client: AsyncClient, catalog: dict[str, Any]) -> None:
    by_price = ok(await client.get(f"{PUBLIC}/products", params={"sort": "price_asc"}))
    assert [i["price"] for i in by_price["items"]] == ["5.00", "9.00", "10.00", "15.00"]
    by_price_desc = ok(await client.get(f"{PUBLIC}/products", params={"sort": "price_desc", "page_size": 2, "page": 2}))
    assert [i["price"] for i in by_price_desc["items"]] == ["9.00", "5.00"] and by_price_desc["total"] == 4
    newest = ok(await client.get(f"{PUBLIC}/products"))
    assert newest["items"][0]["name"] == "Shampoo"
    beyond = ok(await client.get(f"{PUBLIC}/products", params={"page": 9}))
    assert beyond["items"] == [] and beyond["total"] == 4


# ------------------------------------------------------------------------------ cards


async def test_card_fields(client: AsyncClient, catalog: dict[str, Any]) -> None:
    items = {i["name"]: i for i in ok(await client.get(f"{PUBLIC}/products"))["items"]}
    gloss = items["Gloss"]
    assert (gloss["price"], gloss["compare_at_price"], gloss["discount_percent"]) == ("15.00", "20.00", 25)
    assert gloss["is_on_sale"] is True and gloss["in_stock"] is False
    assert gloss["brand"] == {"id": catalog["bella"]["id"], "name": "Bella", "slug": "bella"}
    assert gloss["category"]["slug"] == "lips"
    assert set(gloss["image"]) == {"thumb", "medium", "large"} and gloss["swatches"] == []
    matte = items["Matte Lip"]
    assert (matte["compare_at_price"], matte["discount_percent"], matte["is_new"]) == (None, None, True)
    shades = items["Shades"]
    assert (shades["price"], shades["compare_at_price"], shades["discount_percent"]) == ("9.00", "12.00", 25)
    assert shades["swatches"] == [{"name": "أحمر", "hex": "#B3202C"}, {"name": "وردي", "hex": "#FFC0CB"}]
    assert items["Shampoo"]["brand"] is None
    assert all("stock" not in i for i in items.values())


async def test_swatches_limited_to_8_active(client: AsyncClient, admin_headers: dict[str, str], catalog: dict[str, Any]) -> None:
    variants = [variant(f"لون {i}", f"#0000{i:02d}", stock=1, sort_order=i) for i in range(10)]
    variants[0]["is_active"] = False
    p = await create_product(client, admin_headers, type="variable", name="Many", category_id=catalog["top"]["id"], variants=variants)
    ok(await upload_images(client, admin_headers, p["id"]), 201)
    await publish(client, admin_headers, p["id"])
    card = next(i for i in ok(await client.get(f"{PUBLIC}/products"))["items"] if i["name"] == "Many")
    assert [s["name"] for s in card["swatches"]] == [f"لون {i}" for i in range(1, 9)]


# ----------------------------------------------------------------------------- detail


async def test_detail_simple_and_stock_display(
    client: AsyncClient, admin_headers: dict[str, str], catalog: dict[str, Any], db_session: AsyncSession
) -> None:
    detail = ok(await client.get(f"{PUBLIC}/products/{catalog['a']['slug']}"))
    assert [c["slug"] for c in detail["breadcrumb"]] == ["beauty", "lips"]
    assert [(a["key"], a["display"]) for a in detail["attributes"]] == [
        ("finish", "مطفي"), ("skin", "جافة، دهنية"), ("spf", "15"), ("vegan", "نعم"),
    ]
    assert (detail["regular_price"], detail["sale_price"], detail["current_price"]) == ("10.00", None, "10.00")
    assert (detail["stock_status"], detail["stock_left"]) == ("in_stock", None)
    assert "stock" not in detail and detail["variants"] == []

    shampoo = ok(await client.get(f"{PUBLIC}/products/{catalog['d']['slug']}"))
    assert (shampoo["stock_status"], shampoo["stock_left"]) == ("low", 2)
    gloss = ok(await client.get(f"{PUBLIC}/products/{catalog['b']['slug']}"))
    assert (gloss["stock_status"], gloss["stock_left"]) == ("out", None)

    # The test database has no store settings (default threshold 3); set it to 1.
    db_session.add(StoreSetting(key="low_stock_threshold", value=1))
    await db_session.commit()
    assert ok(await client.get(f"{PUBLIC}/products/{catalog['d']['slug']}"))["stock_status"] == "in_stock"


async def test_detail_variable(client: AsyncClient, catalog: dict[str, Any]) -> None:
    detail = ok(await client.get(f"{PUBLIC}/products/{catalog['c']['slug']}"))
    assert detail["variant_options"] == [
        {"key": "color", "label": "اللون", "type": "color",
         "values": [{"value": "أحمر", "hex": "#B3202C"}, {"value": "وردي", "hex": "#FFC0CB"}]},
    ]
    red, pink = detail["variants"]
    assert (red["price"], red["sale_price"], red["current_price"], red["discount_percent"]) == ("12.00", "9.00", "9.00", 25)
    assert (red["stock_status"], red["stock_left"]) == ("in_stock", None)
    assert (pink["stock_status"], pink["stock_left"]) == ("out", None)
    assert "sku" not in red and "stock" not in red
    assert detail["regular_price"] is None


async def test_images_per_color(client: AsyncClient, admin_headers: dict[str, str], catalog: dict[str, Any]) -> None:
    await add_variant_option(client, admin_headers, catalog["top"]["id"], key="size", label="الحجم", type="select", options=["50 ml", "100 ml"])
    p = await create_product(client, admin_headers, type="variable", name="Perfume", category_id=catalog["top"]["id"], variants=[
        {"options": {"color": "أحمر", "color_hex": "#FF0000", "size": "50 ml"}, "price": "10", "stock": 3},
        {"options": {"color": "أحمر", "color_hex": "#FF0000", "size": "100 ml"}, "price": "15", "stock": 3},
        {"options": {"color": "أزرق", "color_hex": "#0000FF", "size": "50 ml"}, "price": "10", "stock": 3},
    ])
    red50, red100, blue50 = p["variants"]
    general = ok(await upload_images(client, admin_headers, p["id"]), 201)["images"]
    red_upload = ok(await upload_images(client, admin_headers, p["id"], 2, color="أحمر"), 201)
    assert red_upload["color"] == "أحمر" and all(i["variant_id"] == red50["id"] for i in red_upload["images"])
    admin = ok(await client.get(f"{ADMIN}/products/{p['id']}", headers=admin_headers))
    assert admin["warnings"] == [{"code": "COLOR_WITHOUT_IMAGES", "colors": ["أزرق"]}]
    via_variant = ok(await upload_images(client, admin_headers, p["id"], variant_id=blue50["id"]), 201)
    assert via_variant["color"] == "أزرق"
    assert ok(await client.get(f"{ADMIN}/products/{p['id']}", headers=admin_headers))["warnings"] == []
    await publish(client, admin_headers, p["id"])

    detail = ok(await client.get(f"{PUBLIC}/products/{p['slug']}"))
    colors = {i["id"]: i["color"] for i in detail["images"]}
    red_ids = [i["id"] for i in red_upload["images"]]
    general_ids = [i["id"] for i in general]
    assert [colors[i] for i in red_ids] == ["أحمر", "أحمر"] and colors[general_ids[0]] is None
    by_id = {v["id"]: v for v in detail["variants"]}
    assert by_id[red50["id"]]["image_ids"] == red_ids + general_ids
    assert by_id[red100["id"]]["image_ids"] == red_ids + general_ids  # same color, other size
    assert by_id[blue50["id"]]["image_ids"] == [via_variant["images"][0]["id"], *general_ids]

    missing = await upload_images(client, admin_headers, p["id"], color="أخضر")
    assert missing.status_code == 404 and code_of(missing) == "VARIANT_NOT_FOUND"


async def test_detail_bundle(client: AsyncClient, admin_headers: dict[str, str], catalog: dict[str, Any]) -> None:
    red = catalog["c"]["variants"][0]
    box = await create_product(client, admin_headers, type="bundle", name="Gift box", category_id=catalog["other"]["id"], price="25",
                               bundle_items=[{"product_id": catalog["a"]["id"], "quantity": 2}, {"product_id": catalog["c"]["id"], "variant_id": red["id"]}])
    ok(await upload_images(client, admin_headers, box["id"]), 201)
    await publish(client, admin_headers, box["id"])
    detail = ok(await client.get(f"{PUBLIC}/products/{box['slug']}"))
    assert [(i["product"]["name"], i["variant_label"], i["quantity"]) for i in detail["items"]] == [("Matte Lip", None, 2), ("Shades", "أحمر", 1)]
    assert detail["items"][0]["product"]["image"] is not None
    assert (detail["stock_status"], detail["current_price"]) == ("in_stock", "25.00")  # min(10 // 2, 5)


async def test_related(client: AsyncClient, admin_headers: dict[str, str], catalog: dict[str, Any]) -> None:
    related = ok(await client.get(f"{PUBLIC}/products/{catalog['a']['slug']}/related"))
    # Same category has only Gloss (out of stock), so it falls back to the parent tree.
    assert [r["name"] for r in related] == ["Shades"]
    assert (await client.get(f"{PUBLIC}/products/{catalog['a']['slug']}/related", params={"limit": 13})).status_code == 422


# ------------------------------------------------------------------------ query budget


async def test_query_budget(client: AsyncClient, admin_headers: dict[str, str], catalog: dict[str, Any], count_queries: Any) -> None:
    for i in range(6):
        await published_product(client, admin_headers, name=f"Extra {i}", category_id=catalog["child"]["id"], brand_id=catalog["luma"]["id"])

    with count_queries() as small:
        ok(await client.get(f"{PUBLIC}/products", params={"page_size": 2}))
    with count_queries() as large:
        page = ok(await client.get(f"{PUBLIC}/products", params={"page_size": 50, "category": "beauty", "attr.finish": "مطفي", "opt.color": "أحمر"}))
    with count_queries() as full:
        ok(await client.get(f"{PUBLIC}/products", params={"page_size": 50}))
    with count_queries() as full_sorted:
        ok(await client.get(f"{PUBLIC}/products", params={"page_size": 100, "sort": "price_asc"}))
    # Bounded regardless of page size (cards, images and variants are each one batched query).
    for counter in (small, large, full, full_sorted):
        assert counter.count <= 6, counter.statements
    assert full.count == full_sorted.count
    assert page["total"] == 0

    with count_queries() as detail_variable:
        ok(await client.get(f"{PUBLIC}/products/{catalog['c']['slug']}"))
    with count_queries() as detail_simple:
        ok(await client.get(f"{PUBLIC}/products/{catalog['a']['slug']}"))
    with count_queries() as tree:
        ok(await client.get(f"{PUBLIC}/categories"))
    with count_queries() as facets:
        ok(await client.get(f"{PUBLIC}/products/facets", params={"category": "beauty", "attr.finish": "مطفي"}))
    assert detail_variable.count <= 6 and detail_simple.count <= 6, detail_variable.statements
    assert tree.count <= 2 and facets.count <= 6, facets.statements


# ----------------------------------------------------------------------------- facets


async def test_facets_counts_and_self_exclusion(client: AsyncClient, catalog: dict[str, Any]) -> None:
    facets = ok(await client.get(f"{PUBLIC}/products/facets", params={"category": "beauty"}))
    # Ordered by sort_order, then name.
    assert facets["brands"] == [{"slug": "bella", "name": "Bella", "count": 1}, {"slug": "luma", "name": "Luma", "count": 2}]
    assert facets["price"] == {"min": "9.00", "max": "15.00"}
    by_key = {a["key"]: a for a in facets["attributes"]}
    assert set(by_key) == {"finish", "skin", "spf", "vegan"}  # text attribute is not a facet
    assert by_key["finish"]["values"] == [{"value": "مطفي", "count": 1, "hex": None}, {"value": "لامع", "count": 1, "hex": None}, {"value": "ساتان", "count": 1, "hex": None}]
    assert {v["value"]: v["count"] for v in by_key["skin"]["values"]} == {"جافة": 2, "دهنية": 1, "مختلطة": 1}
    assert (by_key["spf"]["min"], by_key["spf"]["max"]) == (15.0, 50.0)
    assert {v["value"]: v["count"] for v in by_key["vegan"]["values"]} == {"true": 1, "false": 1}
    assert facets["options"] == [{"key": "color", "label": "اللون", "type": "color", "values": [
        {"value": "أحمر", "count": 1, "hex": "#B3202C"}, {"value": "وردي", "count": 1, "hex": "#FFC0CB"}]}]

    # Choosing a brand still lists the other brands (own filter ignored) but narrows the rest.
    narrowed = ok(await client.get(f"{PUBLIC}/products/facets", params=[("category", "beauty"), ("brand", "bella")]))
    assert [b["slug"] for b in narrowed["brands"]] == ["bella", "luma"]
    narrowed_finish = next(a for a in narrowed["attributes"] if a["key"] == "finish")
    assert narrowed_finish["values"] == [{"value": "لامع", "count": 1, "hex": None}]
    assert narrowed["price"] == {"min": "15.00", "max": "15.00"}
    assert narrowed["options"][0]["values"] == []

    # Choosing a finish keeps all finishes (own filter ignored); selected values stay listed.
    by_finish = ok(await client.get(f"{PUBLIC}/products/facets", params={"category": "beauty", "attr.finish": "مطفي"}))
    finish = next(a for a in by_finish["attributes"] if a["key"] == "finish")
    assert {v["value"] for v in finish["values"]} == {"مطفي", "لامع", "ساتان"}
    assert [b["count"] for b in by_finish["brands"]] == [1]
    by_price = ok(await client.get(f"{PUBLIC}/products/facets", params={"category": "beauty", "max_price": "9"}))
    assert by_price["price"] == {"min": "9.00", "max": "15.00"}  # price facet ignores the price filter
    by_color = ok(await client.get(f"{PUBLIC}/products/facets", params={"category": "beauty", "opt.color": "وردي"}))
    assert {v["value"] for v in by_color["options"][0]["values"]} == {"أحمر", "وردي"}
    assert by_color["brands"] == [{"slug": "luma", "name": "Luma", "count": 1}]


async def test_facets_without_category(client: AsyncClient, catalog: dict[str, Any]) -> None:
    facets = ok(await client.get(f"{PUBLIC}/products/facets"))
    assert facets["attributes"] == [] and facets["options"] == []
    assert facets["price"] == {"min": "5.00", "max": "15.00"} and len(facets["brands"]) == 2
