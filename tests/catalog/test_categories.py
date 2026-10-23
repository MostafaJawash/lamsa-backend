import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.products.models import Product
from catalog_helpers import (
    ADMIN,
    PUBLIC,
    add_attribute,
    add_variant_option,
    code_of,
    create_category,
    create_product,
    ok,
    png,
    published_product,
    variant,
)

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------- structure


async def test_depth_rules(client: AsyncClient, admin_headers: dict[str, str]) -> None:
    top = await create_category(client, admin_headers, "Top")
    child = await create_category(client, admin_headers, "Child", parent_id=top["id"])

    grandchild = await client.post(f"{ADMIN}/categories", json={"name": "GC", "parent_id": child["id"]}, headers=admin_headers)
    assert grandchild.status_code == 422 and code_of(grandchild) == "CATEGORY_DEPTH_EXCEEDED"

    own_parent = await client.patch(f"{ADMIN}/categories/{top['id']}", json={"parent_id": top["id"]}, headers=admin_headers)
    assert code_of(own_parent) == "CATEGORY_DEPTH_EXCEEDED"

    other = await create_category(client, admin_headers, "Other")
    has_children = await client.patch(f"{ADMIN}/categories/{top['id']}", json={"parent_id": other["id"]}, headers=admin_headers)
    assert code_of(has_children) == "CATEGORY_DEPTH_EXCEEDED"

    moved = ok(await client.patch(f"{ADMIN}/categories/{other['id']}", json={"parent_id": top["id"]}, headers=admin_headers))
    assert moved["parent_id"] == top["id"]


async def test_slug_rules(client: AsyncClient, admin_headers: dict[str, str]) -> None:
    first = await create_category(client, admin_headers, "Skin Care")
    second = await create_category(client, admin_headers, "Skin care")
    assert (first["slug"], second["slug"]) == ("skin-care", "skin-care-2")

    arabic = await create_category(client, admin_headers, "عطور")
    assert arabic["slug"] and arabic["slug"].islower()
    symbols = await create_category(client, admin_headers, "؟؟؟")
    assert symbols["slug"].startswith("category-")

    taken = await client.post(f"{ADMIN}/categories", json={"name": "X", "slug": "skin-care"}, headers=admin_headers)
    assert taken.status_code == 409 and code_of(taken) == "SLUG_TAKEN"
    for bad in ("Bad Slug", "a", "UPPER", "a_b", "-x"):
        response = await client.post(f"{ADMIN}/categories", json={"name": "X", "slug": bad}, headers=admin_headers)
        assert response.status_code == 422, bad


async def test_delete_rules(client: AsyncClient, admin_headers: dict[str, str]) -> None:
    top = await create_category(client, admin_headers, "Top")
    child = await create_category(client, admin_headers, "Child", parent_id=top["id"])
    await create_product(client, admin_headers, name="P", category_id=child["id"])

    blocked = await client.delete(f"{ADMIN}/categories/{top['id']}", headers=admin_headers)
    assert blocked.status_code == 409 and code_of(blocked) == "CATEGORY_NOT_EMPTY"
    assert blocked.json()["error"]["details"] == {"children": 1, "products": 0}
    blocked_child = await client.delete(f"{ADMIN}/categories/{child['id']}", headers=admin_headers)
    assert blocked_child.json()["error"]["details"] == {"children": 0, "products": 1}

    empty = await create_category(client, admin_headers, "Empty")
    assert (await client.delete(f"{ADMIN}/categories/{empty['id']}", headers=admin_headers)).status_code == 204
    missing = await client.get(f"{ADMIN}/categories/{empty['id']}", headers=admin_headers)
    assert missing.status_code == 404 and code_of(missing) == "CATEGORY_NOT_FOUND"


async def test_visibility_of_categories(client: AsyncClient, admin_headers: dict[str, str]) -> None:
    top = await create_category(client, admin_headers, "Visible Top")
    child = await create_category(client, admin_headers, "Visible Child", parent_id=top["id"])
    await published_product(client, admin_headers, name="In child", category_id=child["id"])

    tree = ok(await client.get(f"{PUBLIC}/categories"))
    node = next(c for c in tree if c["id"] == top["id"])
    assert node["product_count"] == 1 and [c["id"] for c in node["children"]] == [child["id"]]

    ok(await client.patch(f"{ADMIN}/categories/{top['id']}", json={"is_active": False}, headers=admin_headers))
    assert top["id"] not in [c["id"] for c in ok(await client.get(f"{PUBLIC}/categories"))]
    # The child is active but its parent is not: invisible.
    response = await client.get(f"{PUBLIC}/categories/{child['slug']}")
    assert response.status_code == 404 and code_of(response) == "CATEGORY_NOT_FOUND"
    assert ok(await client.get(f"{PUBLIC}/products"))["total"] == 0
    # Admins still see everything.
    admin_tree = ok(await client.get(f"{ADMIN}/categories", headers=admin_headers))
    assert next(c for c in admin_tree if c["id"] == top["id"])["product_count"] == 1


async def test_public_detail_with_effective_definitions(
    client: AsyncClient, admin_headers: dict[str, str], base_categories: dict[str, int]
) -> None:
    detail = ok(await client.get(f"{PUBLIC}/categories/perfumes"))
    assert [c["slug"] for c in detail["breadcrumb"]] == ["care", "perfumes"]
    # Inherited from care (skin_type) plus its own filterable concentration; ingredients/notes are not filterable.
    assert [a["key"] for a in detail["attributes"]] == ["skin_type", "concentration"]
    assert [o["key"] for o in detail["variant_options"]] == ["size"]


async def test_child_definition_overrides_parent(
    client: AsyncClient, admin_headers: dict[str, str], base_categories: dict[str, int]
) -> None:
    perfumes = base_categories["perfumes"]
    await add_attribute(
        client, admin_headers, perfumes, key="skin_type", label="نوع البشرة للعطر", type="select",
        options=["عادية"], is_filterable=True,
    )
    detail = ok(await client.get(f"{ADMIN}/categories/{perfumes}", headers=admin_headers))
    skin = next(a for a in detail["effective_attributes"] if a["key"] == "skin_type")
    assert skin["inherited"] is False and skin["options"] == ["عادية"]
    ingredients = next(a for a in detail["effective_attributes"] if a["key"] == "ingredients")
    assert ingredients["inherited"] is True

    # Validation uses the child's definition.
    bad = await client.post(
        f"{ADMIN}/products",
        json={"type": "simple", "name": "P", "category_id": perfumes, "price": "5", "stock": 1,
              "attributes": {"skin_type": "جافة"}},
        headers=admin_headers,
    )
    assert code_of(bad) == "INVALID_ATTRIBUTE_VALUE"


# --------------------------------------------------------------------------- definitions


async def test_attribute_key_rules(client: AsyncClient, admin_headers: dict[str, str]) -> None:
    category = await create_category(client, admin_headers, "Defs")
    await add_attribute(client, admin_headers, category["id"], key="cocoa_percent", label="الكاكاو", type="number", unit="%")
    taken = await client.post(
        f"{ADMIN}/categories/{category['id']}/attributes",
        json={"key": "cocoa_percent", "label": "x", "type": "text"}, headers=admin_headers,
    )
    assert taken.status_code == 409 and code_of(taken) == "ATTRIBUTE_KEY_TAKEN"
    for key in ("Cocoa", "1abc", "a", "a-b", "x" * 51):
        response = await client.post(
            f"{ADMIN}/categories/{category['id']}/attributes", json={"key": key, "label": "x", "type": "text"},
            headers=admin_headers,
        )
        assert response.status_code == 422, key
    attr_id = (await add_attribute(client, admin_headers, category["id"], key="shade", label="x", type="text"))["id"]
    immutable = await client.patch(
        f"{ADMIN}/categories/{category['id']}/attributes/{attr_id}", json={"key": "other"}, headers=admin_headers
    )
    assert immutable.status_code == 422


async def test_select_options_rules(client: AsyncClient, admin_headers: dict[str, str]) -> None:
    category = await create_category(client, admin_headers, "Opts")
    url = f"{ADMIN}/categories/{category['id']}/attributes"
    for body in (
        {"key": "finish", "label": "x", "type": "select"},
        {"key": "finish", "label": "x", "type": "select", "options": []},
        {"key": "finish", "label": "x", "type": "multiselect", "options": ["a", "a"]},
        {"key": "finish", "label": "x", "type": "text", "options": ["a"]},
    ):
        assert (await client.post(url, json=body, headers=admin_headers)).status_code == 422, body


async def test_type_change_blocked_when_in_use(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    top = await create_category(client, admin_headers, "Tree")
    child = await create_category(client, admin_headers, "Leaf", parent_id=top["id"])
    attr = await add_attribute(client, admin_headers, top["id"], key="weight", label="الوزن", type="number", unit="g")
    url = f"{ADMIN}/categories/{top['id']}/attributes/{attr['id']}"
    assert ok(await client.patch(url, json={"type": "text"}, headers=admin_headers))["type"] == "text"
    ok(await client.patch(url, json={"type": "number"}, headers=admin_headers))

    await create_product(client, admin_headers, name="P", category_id=child["id"], attributes={"weight": 50})
    response = await client.patch(url, json={"type": "text"}, headers=admin_headers)
    assert response.status_code == 409 and code_of(response) == "ATTRIBUTE_IN_USE"
    assert response.json()["error"]["details"]["products_count"] == 1
    assert ok(await client.patch(url, json={"label": "الوزن الصافي"}, headers=admin_headers))["label"] == "الوزن الصافي"


async def test_option_value_removal_in_use(client: AsyncClient, admin_headers: dict[str, str]) -> None:
    category = await create_category(client, admin_headers, "Finish")
    single = await add_attribute(client, admin_headers, category["id"], key="finish", label="x", type="select", options=["matte", "glossy"])
    multi = await add_attribute(client, admin_headers, category["id"], key="tags", label="x", type="multiselect", options=["a", "b", "c"])
    await create_product(client, admin_headers, name="P", category_id=category["id"], attributes={"finish": "matte", "tags": ["a", "b"]})

    url = f"{ADMIN}/categories/{category['id']}/attributes"
    blocked = await client.patch(f"{url}/{single['id']}", json={"options": ["glossy"]}, headers=admin_headers)
    assert blocked.status_code == 409 and code_of(blocked) == "OPTION_IN_USE"
    assert blocked.json()["error"]["details"] == {"value": "matte", "products_count": 1}
    assert ok(await client.patch(f"{url}/{single['id']}", json={"options": ["matte", "satin"]}, headers=admin_headers))

    blocked_multi = await client.patch(f"{url}/{multi['id']}", json={"options": ["a", "c"]}, headers=admin_headers)
    assert code_of(blocked_multi) == "OPTION_IN_USE" and blocked_multi.json()["error"]["details"]["value"] == "b"
    assert ok(await client.patch(f"{url}/{multi['id']}", json={"options": ["a", "b"]}, headers=admin_headers))


async def test_delete_attribute_in_use_and_force(
    client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession
) -> None:
    category = await create_category(client, admin_headers, "Del")
    attr = await add_attribute(client, admin_headers, category["id"], key="origin", label="x", type="text")
    product = await create_product(client, admin_headers, name="P", category_id=category["id"], attributes={"origin": "Syria"})
    url = f"{ADMIN}/categories/{category['id']}/attributes/{attr['id']}"

    blocked = await client.delete(url, headers=admin_headers)
    assert blocked.status_code == 409 and code_of(blocked) == "ATTRIBUTE_IN_USE"
    assert (await client.delete(f"{url}?force=true", headers=admin_headers)).status_code == 204

    stored = await db_session.scalar(
        select(Product.attributes).where(Product.id == product["id"]).execution_options(populate_existing=True)
    )
    assert stored == {}
    missing = await client.delete(url, headers=admin_headers)
    assert missing.status_code == 404 and code_of(missing) == "ATTRIBUTE_NOT_FOUND"


async def test_variant_option_definition_rules(client: AsyncClient, admin_headers: dict[str, str]) -> None:
    category = await create_category(client, admin_headers, "Sizes")
    size = await add_variant_option(client, admin_headers, category["id"], key="size", label="الحجم", type="select", options=["30 ml", "50 ml"])
    taken = await client.post(
        f"{ADMIN}/categories/{category['id']}/variant-options", json={"key": "size", "label": "x", "type": "select"},
        headers=admin_headers,
    )
    assert code_of(taken) == "ATTRIBUTE_KEY_TAKEN"
    color_with_options = await client.post(
        f"{ADMIN}/categories/{category['id']}/variant-options",
        json={"key": "color", "label": "x", "type": "color", "options": ["red"]}, headers=admin_headers,
    )
    assert color_with_options.status_code == 422

    await create_product(
        client, admin_headers, type="variable", name="Perfume", category_id=category["id"],
        variants=[{"options": {"size": "30 ml"}, "price": "10"}],
    )
    url = f"{ADMIN}/categories/{category['id']}/variant-options/{size['id']}"
    type_change = await client.patch(url, json={"type": "color"}, headers=admin_headers)
    assert type_change.status_code == 409 and code_of(type_change) == "VARIANT_OPTION_IN_USE"
    remove_used = await client.patch(url, json={"options": ["50 ml"]}, headers=admin_headers)
    assert remove_used.status_code == 409 and code_of(remove_used) == "OPTION_IN_USE"
    assert ok(await client.patch(url, json={"options": ["30 ml", "100 ml"]}, headers=admin_headers))
    delete_used = await client.delete(url, headers=admin_headers)
    assert delete_used.status_code == 409 and code_of(delete_used) == "VARIANT_OPTION_IN_USE"


async def test_moving_category_revalidates_products(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    parent_a = await create_category(client, admin_headers, "A")
    await add_attribute(client, admin_headers, parent_a["id"], key="grade", label="x", type="text")
    parent_b = await create_category(client, admin_headers, "B")
    child = await create_category(client, admin_headers, "Movable", parent_id=parent_a["id"])
    await create_product(client, admin_headers, name="P", category_id=child["id"], attributes={"grade": "A+"})

    response = await client.patch(f"{ADMIN}/categories/{child['id']}", json={"parent_id": parent_b["id"]}, headers=admin_headers)
    assert response.status_code == 422 and code_of(response) == "UNKNOWN_ATTRIBUTE"
    assert "product_id" in response.json()["error"]["details"]
    assert ok(await client.get(f"{ADMIN}/categories/{child['id']}", headers=admin_headers))["parent_id"] == parent_a["id"]


async def test_category_image_replaced_files_removed_after_commit(
    client: AsyncClient, admin_headers: dict[str, str], media_root
) -> None:
    category = await create_category(client, admin_headers, "Pics")
    url = f"{ADMIN}/categories/{category['id']}/image"
    first = ok(await client.put(url, files={"file": ("a.png", png(), "image/png")}, headers=admin_headers))
    first_files = sorted(p.name for p in (media_root / "categories" / str(category["id"])).iterdir())
    assert len(first_files) == 3 and first["image"]["medium"].endswith("_medium.webp")

    ok(await client.put(url, files={"file": ("b.png", png((1, 2, 3)), "image/png")}, headers=admin_headers))
    second_files = sorted(p.name for p in (media_root / "categories" / str(category["id"])).iterdir())
    assert len(second_files) == 3 and not set(first_files) & set(second_files)

    cleared = ok(await client.delete(url, headers=admin_headers))
    assert cleared["image"] is None
    assert list((media_root / "categories" / str(category["id"])).iterdir()) == []


async def test_category_rename_updates_search_text(
    client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession
) -> None:
    top = await create_category(client, admin_headers, "Gifts")
    child = await create_category(client, admin_headers, "Boxes", parent_id=top["id"])
    product = await create_product(client, admin_headers, name="Box", category_id=child["id"])
    ok(await client.patch(f"{ADMIN}/categories/{top['id']}", json={"name": "هدايا"}, headers=admin_headers))
    text = await db_session.scalar(
        select(Product.search_text).where(Product.id == product["id"]).execution_options(populate_existing=True)
    )
    assert "هدايا" in (text or "")
    assert variant  # helper import kept for symmetry with other tests
