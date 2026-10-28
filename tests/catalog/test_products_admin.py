from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.orders.models import Order, OrderItem
from app.modules.products.models import Product
from catalog_helpers import (
    ADMIN,
    add_attribute,
    add_variant_option,
    admin_product,
    code_of,
    create_brand,
    create_category,
    create_product,
    ok,
    publish,
    upload_images,
    variant,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def shop(client: AsyncClient, admin_headers: dict[str, str]) -> dict[str, int]:
    """A category with every attribute type and a color + size option."""
    category = await create_category(client, admin_headers, "Shop")
    cid = category["id"]
    await add_attribute(client, admin_headers, cid, key="notes", label="ملاحظات", type="text")
    await add_attribute(client, admin_headers, cid, key="cocoa", label="كاكاو", type="number", unit="%", is_filterable=True)
    await add_attribute(client, admin_headers, cid, key="finish", label="اللمسة", type="select", options=["مطفي", "لامع"], is_filterable=True)
    await add_attribute(client, admin_headers, cid, key="tags", label="وسوم", type="multiselect", options=["a", "b", "c"], is_filterable=True)
    await add_attribute(client, admin_headers, cid, key="vegan", label="نباتي", type="boolean", is_filterable=True)
    await add_attribute(client, admin_headers, cid, key="expiry", label="انتهاء", type="date")
    await add_variant_option(client, admin_headers, cid, key="color", label="اللون", type="color")
    await add_variant_option(client, admin_headers, cid, key="size", label="الحجم", type="select", options=["50 ml", "100 ml"])
    return {"category": cid}


def post(client: AsyncClient, headers: dict[str, str], body: dict) -> object:
    return client.post(f"{ADMIN}/products", json=body, headers=headers)


# --------------------------------------------------------------------------- types


async def test_type_field_rules(client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, int]) -> None:
    cid = shop["category"]
    cases = [
        ({"type": "variable", "price": "5"}, "price"),
        ({"type": "variable", "stock": 3}, "stock"),
        ({"type": "variable", "sale_price": "1"}, "sale_price"),
        ({"type": "bundle", "price": "5", "stock": 1}, "stock"),
        ({"type": "simple", "price": "5", "stock": 1, "variants": [variant("أحمر")]}, "variants"),
        ({"type": "simple", "price": "5", "stock": 1, "bundle_items": []}, "bundle_items"),
        ({"type": "bundle", "price": "5", "variants": [variant("أحمر")]}, "variants"),
    ]
    for body, field in cases:
        response = await post(client, admin_headers, {"name": "X", "category_id": cid, **body})
        assert response.status_code == 422 and code_of(response) == "INVALID_PRODUCT_FOR_TYPE", body
        assert response.json()["error"]["details"] == {"field": field, "type": body["type"]}

    for body in ({"type": "simple", "stock": 1}, {"type": "simple", "price": "5"}, {"type": "bundle"}):
        response = await post(client, admin_headers, {"name": "X", "category_id": cid, **body})
        assert response.status_code == 422 and code_of(response) == "VALIDATION_ERROR", body


async def test_type_is_immutable_and_new_products_inactive(
    client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, int]
) -> None:
    product = await create_product(client, admin_headers, name="P", category_id=shop["category"], is_active=True)
    assert product["is_active"] is False
    response = await client.patch(f"{ADMIN}/products/{product['id']}", json={"type": "bundle"}, headers=admin_headers)
    assert response.status_code == 422 and code_of(response) == "PRODUCT_TYPE_IMMUTABLE"
    same = await client.patch(f"{ADMIN}/products/{product['id']}", json={"type": "simple", "name": "Q"}, headers=admin_headers)
    assert ok(same)["name"] == "Q"


async def test_money_and_sale_price_rules(client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, int]) -> None:
    cid = shop["category"]
    for bad in ("1.234", "-1", "10000000000000.00", "abc"):
        response = await post(client, admin_headers, {"type": "simple", "name": "X", "category_id": cid, "price": bad, "stock": 1})
        assert response.status_code == 422, bad
    for sale in ("10.00", "12.00"):
        response = await post(client, admin_headers, {"type": "simple", "name": "X", "category_id": cid, "price": "10", "stock": 1, "sale_price": sale})
        assert code_of(response) == "VALIDATION_ERROR" and response.json()["error"]["details"][0]["field"] == "sale_price"

    product = await create_product(client, admin_headers, name="P", category_id=cid, price=8.5, sale_price="7.25")
    assert (product["price"], product["sale_price"]) == ("8.50", "7.25")
    lower = await client.patch(f"{ADMIN}/products/{product['id']}", json={"price": "7.00"}, headers=admin_headers)
    assert code_of(lower) == "VALIDATION_ERROR"  # would make sale_price >= price
    bad_variant = await post(client, admin_headers, {
        "type": "variable", "name": "V", "category_id": cid,
        "variants": [variant("أحمر", price="5", sale_price="5")],
    })
    assert code_of(bad_variant) == "VALIDATION_ERROR"


async def test_bundle_rejects_components_in_another_currency(
    client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, int]
) -> None:
    component = await create_product(
        client,
        admin_headers,
        name="USD component",
        category_id=shop["category"],
        currency="USD",
        price="13.00",
    )
    response = await client.post(
        f"{ADMIN}/products",
        json={
            "type": "bundle",
            "name": "SYP bundle",
            "category_id": shop["category"],
            "currency": "SYP",
            "price": "150000.00",
            "bundle_items": [{"product_id": component["id"], "quantity": 1}],
        },
        headers=admin_headers,
    )
    assert response.status_code == 422
    assert code_of(response) == "BUNDLE_CURRENCY_MISMATCH"


# ----------------------------------------------------------------------- attributes


async def test_attribute_value_rules(client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, int]) -> None:
    cid = shop["category"]
    good = await create_product(client, admin_headers, name="Good", category_id=cid, attributes={
        "notes": "  ناعم  ", "cocoa": "70.5", "finish": "مطفي", "tags": ["a", "c"], "vegan": True,
        "expiry": "2027-01-31", "ignored": None,
    })
    assert good["attributes"] == {"notes": "ناعم", "cocoa": 70.5, "finish": "مطفي", "tags": ["a", "c"], "vegan": True, "expiry": "2027-01-31"}

    unknown = await post(client, admin_headers, {"type": "simple", "name": "X", "category_id": cid, "price": "1", "stock": 1, "attributes": {"colour": "x"}})
    assert code_of(unknown) == "UNKNOWN_ATTRIBUTE" and unknown.json()["error"]["details"] == {"key": "colour"}

    invalid = {
        "notes": 5, "cocoa": "lots", "finish": "ساتان", "tags": [], "vegan": "yes", "expiry": "31/01/2027",
    }
    for key, value in invalid.items():
        response = await post(client, admin_headers, {"type": "simple", "name": "X", "category_id": cid, "price": "1", "stock": 1, "attributes": {key: value}})
        assert code_of(response) == "INVALID_ATTRIBUTE_VALUE", key
        assert response.json()["error"]["details"]["key"] == key
    for value in (["a", "a"], ["z"]):
        response = await post(client, admin_headers, {"type": "simple", "name": "X", "category_id": cid, "price": "1", "stock": 1, "attributes": {"tags": value}})
        assert code_of(response) == "INVALID_ATTRIBUTE_VALUE"
    long_text = await post(client, admin_headers, {"type": "simple", "name": "X", "category_id": cid, "price": "1", "stock": 1, "attributes": {"notes": "x" * 1001}})
    assert code_of(long_text) == "INVALID_ATTRIBUTE_VALUE"


async def test_required_attributes_and_patch_replaces(
    client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, int]
) -> None:
    cid = shop["category"]
    product = await create_product(client, admin_headers, name="P", category_id=cid, attributes={"notes": "x", "finish": "لامع"})
    await add_attribute(client, admin_headers, cid, key="origin", label="المنشأ", type="text", is_required=True)

    missing = await client.patch(f"{ADMIN}/products/{product['id']}", json={"attributes": {"notes": "y"}}, headers=admin_headers)
    assert code_of(missing) == "MISSING_REQUIRED_ATTRIBUTE" and missing.json()["error"]["details"] == {"keys": ["origin"]}
    replaced = ok(await client.patch(
        f"{ADMIN}/products/{product['id']}", json={"attributes": {"origin": "سوريا", "finish": None}}, headers=admin_headers
    ))
    assert replaced["attributes"] == {"origin": "سوريا"}  # whole object replaced, null dropped
    empty = await post(client, admin_headers, {"type": "simple", "name": "X", "category_id": cid, "price": "1", "stock": 1, "attributes": {"origin": "  "}})
    assert code_of(empty) == "MISSING_REQUIRED_ATTRIBUTE"


async def test_category_change_revalidates(client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, int]) -> None:
    plain = await create_category(client, admin_headers, "Plain")
    product = await create_product(client, admin_headers, name="P", category_id=shop["category"], attributes={"finish": "مطفي"})
    response = await client.patch(f"{ADMIN}/products/{product['id']}", json={"category_id": plain["id"]}, headers=admin_headers)
    assert code_of(response) == "UNKNOWN_ATTRIBUTE"
    assert (await admin_product(client, admin_headers, product["id"]))["category"]["id"] == shop["category"]

    var = await create_product(client, admin_headers, type="variable", name="V", category_id=shop["category"], variants=[variant("أحمر")])
    response = await client.patch(f"{ADMIN}/products/{var['id']}", json={"category_id": plain["id"]}, headers=admin_headers)
    assert code_of(response) == "INVALID_VARIANT_OPTIONS"
    moved = ok(await client.patch(f"{ADMIN}/products/{product['id']}", json={"category_id": plain["id"], "attributes": {}}, headers=admin_headers))
    assert moved["category"]["id"] == plain["id"]


# ------------------------------------------------------------------------- variants


async def test_variant_option_rules(client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, int]) -> None:
    cid = shop["category"]

    async def create_with(variants: list[dict]) -> object:
        return await post(client, admin_headers, {"type": "variable", "name": "V", "category_id": cid, "variants": variants})

    assert code_of(await create_with([{"options": {"shade": "x"}, "price": "1"}])) == "INVALID_VARIANT_OPTIONS"
    assert code_of(await create_with([{"options": {"color": "أحمر"}, "price": "1"}])) == "INVALID_VARIANT_OPTIONS"  # no hex
    assert code_of(await create_with([variant("أحمر", "red")])) == "INVALID_VARIANT_OPTIONS"  # bad hex
    assert code_of(await create_with([{"options": {"size": "75 ml"}, "price": "1"}])) == "INVALID_VARIANT_OPTIONS"
    assert code_of(await create_with([{"options": {"color": "x" * 51, "color_hex": "#000000"}, "price": "1"}])) == "INVALID_VARIANT_OPTIONS"
    mixed = await create_with([variant("أحمر"), {"options": {"size": "50 ml"}, "price": "1"}])
    assert code_of(mixed) == "INVALID_VARIANT_OPTIONS"
    duplicate = await create_with([variant("أحمر", "#AA0000"), variant("أحمر", "#BB0000")])
    assert duplicate.status_code == 409 and code_of(duplicate) == "DUPLICATE_VARIANT"

    product = await create_product(client, admin_headers, type="variable", name="Two axes", category_id=cid, variants=[
        {"options": {"color": "أحمر", "color_hex": "#b3202c", "size": "50 ml"}, "price": "10"},
        {"options": {"size": "100 ml", "color": "أحمر", "color_hex": "#b3202c"}, "price": "15"},
    ])
    assert product["variants"][0]["options_signature"] == "color=أحمر|size=50 ml"
    assert product["variants"][0]["color"] == "أحمر"
    again = await client.post(
        f"{ADMIN}/products/{product['id']}/variants",
        json={"options": {"color": "أحمر", "color_hex": "#000000", "size": "50 ml"}, "price": "9"}, headers=admin_headers,
    )
    assert again.status_code == 409 and code_of(again) == "DUPLICATE_VARIANT"


async def test_sku_rules(client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, int]) -> None:
    cid = shop["category"]
    simple = await create_product(client, admin_headers, name="S", category_id=cid)
    assert simple["sku"] == f"LM-{simple['id']}"
    named = await create_product(client, admin_headers, name="N", category_id=cid, sku="abc-123")
    assert named["sku"] == "ABC-123"
    for bad in ("ab", "a b c", "x" * 65, "ÄBC"):
        assert (await post(client, admin_headers, {"type": "simple", "name": "X", "category_id": cid, "price": "1", "stock": 1, "sku": bad})).status_code == 422

    var = await create_product(client, admin_headers, type="variable", name="V", category_id=cid, variants=[variant("أحمر", sku="abc-123")] if False else [variant("أحمر")])
    assert var["sku"] is None and var["variants"][0]["sku"] == f"LM-{var['id']}-1"

    # Global uniqueness across products and variants, both directions.
    as_variant = await client.post(f"{ADMIN}/products/{var['id']}/variants", json={**variant("أزرق"), "sku": "ABC-123"}, headers=admin_headers)
    assert as_variant.status_code == 409 and code_of(as_variant) == "SKU_TAKEN"
    as_product = await post(client, admin_headers, {"type": "simple", "name": "X", "category_id": cid, "price": "1", "stock": 1, "sku": var["variants"][0]["sku"]})
    assert as_product.status_code == 409 and code_of(as_product) == "SKU_TAKEN"


async def test_sku_race_maps_integrity_error(
    client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_product(client, admin_headers, name="First", category_id=shop["category"], sku="RACE-1")

    async def never_taken(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("app.modules.products.service.ensure_sku_available", never_taken)
    response = await post(client, admin_headers, {"type": "simple", "name": "Second", "category_id": shop["category"], "price": "1", "stock": 1, "sku": "RACE-1"})
    assert response.status_code == 409 and code_of(response) == "SKU_TAKEN"


async def test_variant_endpoints_on_wrong_product(client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, int]) -> None:
    simple = await create_product(client, admin_headers, name="S", category_id=shop["category"])
    response = await client.post(f"{ADMIN}/products/{simple['id']}/variants", json=variant("أحمر"), headers=admin_headers)
    assert code_of(response) == "INVALID_PRODUCT_FOR_TYPE"
    a = await create_product(client, admin_headers, type="variable", name="A", category_id=shop["category"], variants=[variant("أحمر")])
    b = await create_product(client, admin_headers, type="variable", name="B", category_id=shop["category"], variants=[variant("أحمر")])
    foreign = await client.patch(f"{ADMIN}/products/{a['id']}/variants/{b['variants'][0]['id']}", json={"stock": 1}, headers=admin_headers)
    assert foreign.status_code == 404 and code_of(foreign) == "VARIANT_NOT_FOUND"


async def test_delete_variant_and_product_guards(
    client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, int], db_session: AsyncSession
) -> None:
    cid = shop["category"]
    var = await create_product(client, admin_headers, type="variable", name="V", category_id=cid, variants=[variant("أحمر"), variant("أزرق", "#0000FF")])
    red, blue = var["variants"]
    bundle = await create_product(client, admin_headers, type="bundle", name="Box", category_id=cid,
                                  bundle_items=[{"product_id": var["id"], "variant_id": blue["id"]}])

    in_bundle = await client.delete(f"{ADMIN}/products/{var['id']}/variants/{blue['id']}", headers=admin_headers)
    assert in_bundle.status_code == 409 and code_of(in_bundle) == "VARIANT_IN_BUNDLE"
    product_in_bundle = await client.delete(f"{ADMIN}/products/{var['id']}", headers=admin_headers)
    assert product_in_bundle.status_code == 409 and code_of(product_in_bundle) == "PRODUCT_IN_BUNDLE"

    user_id = (await client.get("/api/v1/users/me", headers=admin_headers)).json()["id"]
    order = Order(user_id=user_id, customer_name="A", customer_phone="+4915100000000", delivery_method_code="home",
                  delivery_method_name="x")
    db_session.add(order)
    await db_session.flush()
    db_session.add(OrderItem(order_id=order.id, product_id=var["id"], variant_id=red["id"], product_name="V",
                             currency="SYP", unit_price=Decimal("10"), quantity=1, line_total=Decimal("10")))
    await db_session.commit()

    has_orders = await client.delete(f"{ADMIN}/products/{var['id']}/variants/{red['id']}", headers=admin_headers)
    assert has_orders.status_code == 409 and code_of(has_orders) == "VARIANT_HAS_ORDERS"
    ok(await client.put(f"{ADMIN}/products/{bundle['id']}/bundle-items", json={"items": []}, headers=admin_headers))
    product_orders = await client.delete(f"{ADMIN}/products/{var['id']}", headers=admin_headers)
    assert product_orders.status_code == 409 and code_of(product_orders) == "PRODUCT_HAS_ORDERS"
    assert ok(await client.delete(f"{ADMIN}/products/{var['id']}/variants/{blue['id']}", headers=admin_headers))

    clean = await create_product(client, admin_headers, name="Clean", category_id=cid)
    assert (await client.delete(f"{ADMIN}/products/{clean['id']}", headers=admin_headers)).status_code == 204
    assert (await client.get(f"{ADMIN}/products/{clean['id']}", headers=admin_headers)).status_code == 404


# -------------------------------------------------------------------------- bundles


async def test_bundle_item_rules(client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, int]) -> None:
    cid = shop["category"]
    simple = await create_product(client, admin_headers, name="S", category_id=cid)
    var = await create_product(client, admin_headers, type="variable", name="V", category_id=cid, variants=[variant("أحمر")])
    other = await create_product(client, admin_headers, type="variable", name="O", category_id=cid, variants=[variant("أحمر")])
    bundle = await create_product(client, admin_headers, type="bundle", name="B", category_id=cid)
    other_bundle = await create_product(client, admin_headers, type="bundle", name="B2", category_id=cid)
    url = f"{ADMIN}/products/{bundle['id']}/bundle-items"

    bad_sets = [
        [{"product_id": bundle["id"]}],
        [{"product_id": other_bundle["id"]}],
        [{"product_id": var["id"]}],
        [{"product_id": var["id"], "variant_id": other["variants"][0]["id"]}],
        [{"product_id": simple["id"], "variant_id": var["variants"][0]["id"]}],
        [{"product_id": simple["id"]}, {"product_id": simple["id"], "quantity": 2}],
        [{"product_id": 999_999}],
    ]
    for items in bad_sets:
        response = await client.put(url, json={"items": items}, headers=admin_headers)
        assert response.status_code == 422 and code_of(response) == "INVALID_BUNDLE_ITEM", items
    for quantity in (0, 100):
        response = await client.put(url, json={"items": [{"product_id": simple["id"], "quantity": quantity}]}, headers=admin_headers)
        assert response.status_code == 422

    good = ok(await client.put(url, json={"items": [
        {"product_id": simple["id"], "quantity": 2},
        {"product_id": var["id"], "variant_id": var["variants"][0]["id"]},
    ]}, headers=admin_headers))
    assert [(i["product_id"], i["quantity"]) for i in good["bundle_items"]] == [(simple["id"], 2), (var["id"], 1)]
    replaced = ok(await client.put(url, json={"items": [{"product_id": simple["id"]}]}, headers=admin_headers))
    assert len(replaced["bundle_items"]) == 1
    wrong = await client.put(f"{ADMIN}/products/{simple['id']}/bundle-items", json={"items": []}, headers=admin_headers)
    assert code_of(wrong) == "INVALID_PRODUCT_FOR_TYPE"


# ------------------------------------------------------------------------ publishing


async def test_publish_check_lists_all_reasons(client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, int]) -> None:
    cid = shop["category"]
    brand = await create_brand(client, admin_headers, "Off", is_active=False)
    var = await create_product(client, admin_headers, type="variable", name="V", category_id=cid, brand_id=brand["id"],
                               variants=[variant("أحمر", is_active=False)])
    await add_attribute(client, admin_headers, cid, key="origin", label="x", type="text", is_required=True)
    ok(await client.patch(f"{ADMIN}/categories/{cid}", json={"is_active": False}, headers=admin_headers))

    response = await client.patch(f"{ADMIN}/products/{var['id']}", json={"is_active": True}, headers=admin_headers)
    assert response.status_code == 422 and code_of(response) == "PRODUCT_NOT_PUBLISHABLE"
    assert response.json()["error"]["details"]["reasons"] == [
        "NO_IMAGES", "MISSING_REQUIRED_ATTRIBUTE", "NO_ACTIVE_VARIANTS", "CATEGORY_INACTIVE", "BRAND_INACTIVE",
    ]
    bundle = await create_product(client, admin_headers, type="bundle", name="B", category_id=cid, attributes={"origin": "x"})
    reasons = (await admin_product(client, admin_headers, bundle["id"]))["publishable"]["reasons"]
    assert "NO_BUNDLE_ITEMS" in reasons

    # Deactivation never fails.
    simple = await create_product(client, admin_headers, name="S", category_id=cid, attributes={"origin": "x"})
    ok(await client.patch(f"{ADMIN}/categories/{cid}", json={"is_active": True}, headers=admin_headers))
    ok(await upload_images(client, admin_headers, simple["id"]), 201)
    assert (await publish(client, admin_headers, simple["id"]))["is_active"] is True
    off = ok(await client.patch(f"{ADMIN}/products/{simple['id']}", json={"is_active": False}, headers=admin_headers))
    assert off["is_active"] is False


async def test_failed_publish_saves_nothing(client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, int], db_session: AsyncSession) -> None:
    product = await create_product(client, admin_headers, name="Original", category_id=shop["category"])
    response = await client.patch(f"{ADMIN}/products/{product['id']}", json={"name": "Renamed", "is_active": True}, headers=admin_headers)
    assert code_of(response) == "PRODUCT_NOT_PUBLISHABLE"
    name = await db_session.scalar(select(Product.name).where(Product.id == product["id"]).execution_options(populate_existing=True))
    assert name == "Original"


# ---------------------------------------------------------------------------- stock


async def test_stock_adjust(client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, int]) -> None:
    product = await create_product(client, admin_headers, name="S", category_id=shop["category"], stock=3)
    url = f"{ADMIN}/products/{product['id']}/stock-adjust"
    assert ok(await client.post(url, json={"delta": 4, "reason": "delivery"}, headers=admin_headers)) == {"stock": 7}
    negative = await client.post(url, json={"delta": -8}, headers=admin_headers)
    assert negative.status_code == 422 and code_of(negative) == "STOCK_NEGATIVE"
    assert ok(await client.post(url, json={"delta": -7}, headers=admin_headers)) == {"stock": 0}
    assert (await client.post(url, json={"delta": 0}, headers=admin_headers)).status_code == 422

    var = await create_product(client, admin_headers, type="variable", name="V", category_id=shop["category"], variants=[variant("أحمر", stock=2)])
    assert code_of(await client.post(f"{ADMIN}/products/{var['id']}/stock-adjust", json={"delta": 1}, headers=admin_headers)) == "INVALID_PRODUCT_FOR_TYPE"
    vurl = f"{ADMIN}/products/{var['id']}/variants/{var['variants'][0]['id']}/stock-adjust"
    assert ok(await client.post(vurl, json={"delta": -2}, headers=admin_headers)) == {"stock": 0}
    assert code_of(await client.post(vurl, json={"delta": -1}, headers=admin_headers)) == "STOCK_NEGATIVE"


async def test_low_stock_and_admin_list(client: AsyncClient, admin_headers: dict[str, str], shop: dict[str, int]) -> None:
    cid = shop["category"]
    low = await create_product(client, admin_headers, name="Low simple", category_id=cid, stock=2)
    await create_product(client, admin_headers, name="Plenty", category_id=cid, stock=50)
    var = await create_product(client, admin_headers, type="variable", name="Var", category_id=cid,
                               variants=[variant("أحمر", stock=1), variant("أزرق", "#0000FF", stock=20)])
    for p in (low, var):
        ok(await upload_images(client, admin_headers, p["id"]), 201)
        await publish(client, admin_headers, p["id"])

    page = ok(await client.get(f"{ADMIN}/products/low-stock", headers=admin_headers))
    assert [(i["product_name"], i["variant_label"], i["stock"]) for i in page["items"]] == [("Var", "أحمر", 1), ("Low simple", None, 2)]
    assert page["items"][0]["sku"] == var["variants"][0]["sku"]
    assert ok(await client.get(f"{ADMIN}/products/low-stock", params={"threshold": 0}, headers=admin_headers))["total"] == 0

    listing = ok(await client.get(f"{ADMIN}/products", params={"low_stock": True, "sort": "name"}, headers=admin_headers))
    assert [i["name"] for i in listing["items"]] == ["Low simple", "Var"]
    by_stock = ok(await client.get(f"{ADMIN}/products", params={"sort": "stock_asc", "type": "simple"}, headers=admin_headers))
    assert [i["stock"] for i in by_stock["items"]] == [2, 50]
    assert ok(await client.get(f"{ADMIN}/products", params={"is_active": False}, headers=admin_headers))["total"] == 1
    assert ok(await client.get(f"{ADMIN}/products", params={"q": var["variants"][1]["sku"]}, headers=admin_headers))["items"][0]["id"] == var["id"]
