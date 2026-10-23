"""A small published shop for cart and favorites tests (built through the admin API)."""

from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.settings.models import StoreSetting
from catalog_helpers import (
    ADMIN,
    add_variant_option,
    create_brand,
    create_category,
    create_product,
    ok,
    publish,
    upload_images,
    variant,
)

CART = "/api/v1/cart"
FAVORITES = "/api/v1/favorites"


async def build_shop(client: AsyncClient, admin: dict[str, str], session: AsyncSession) -> dict[str, Any]:
    from scripts.seed_base import seed_delivery_methods

    await seed_delivery_methods(session)
    await session.commit()

    category = await create_category(client, admin, "Cart Shop", slug="cart-shop")
    await add_variant_option(client, admin, category["id"], key="color", label="اللون", type="color")
    await add_variant_option(client, admin, category["id"], key="size", label="الحجم", type="select", options=["50 ml", "100 ml"])
    brand = await create_brand(client, admin, "Cart Brand", slug="cart-brand")

    simple = await create_product(client, admin, name="Cream", category_id=category["id"], brand_id=brand["id"],
                                  price="10.00", sale_price="8.00", stock=5)
    lip = await create_product(client, admin, type="variable", name="Lipstick", category_id=category["id"], brand_id=brand["id"],
                               variants=[variant("أحمر", "#FF0000", price="12.00", stock=5),
                                         variant("أزرق", "#0000FF", price="12.00", stock=0),
                                         variant("وردي", "#FFC0CB", price="12.00", sale_price="9.00", stock=2)])
    perfume = await create_product(client, admin, type="variable", name="Perfume", category_id=category["id"], variants=[
        {"options": {"color": "أحمر", "color_hex": "#FF0000", "size": "50 ml"}, "price": "20.00", "stock": 4},
        {"options": {"color": "أحمر", "color_hex": "#FF0000", "size": "100 ml"}, "price": "30.00", "stock": 4},
    ])
    general = {}
    color_images = {}
    for product in (simple, lip, perfume):
        general[product["id"]] = ok(await upload_images(client, admin, product["id"]), 201)["images"][0]
    color_images["lip_red"] = ok(await upload_images(client, admin, lip["id"], color="أحمر"), 201)["images"][0]
    color_images["perfume_red"] = ok(await upload_images(client, admin, perfume["id"], color="أحمر"), 201)["images"][0]
    for product in (simple, lip, perfume):
        await publish(client, admin, product["id"])

    red = lip["variants"][0]
    bundle = await create_product(client, admin, type="bundle", name="Box", category_id=category["id"], price="25.00",
                                  bundle_items=[{"product_id": simple["id"], "quantity": 2},
                                                {"product_id": lip["id"], "variant_id": red["id"]}])
    general[bundle["id"]] = ok(await upload_images(client, admin, bundle["id"]), 201)["images"][0]
    await publish(client, admin, bundle["id"])
    return {
        "category": category, "brand": brand, "simple": simple, "lip": lip, "perfume": perfume, "bundle": bundle,
        "red": lip["variants"][0], "blue": lip["variants"][1], "pink": lip["variants"][2],
        "p50": perfume["variants"][0], "p100": perfume["variants"][1],
        "general": general, "color_images": color_images,
    }


async def set_setting(session: AsyncSession, key: str, value: int) -> None:
    existing = await session.get(StoreSetting, key)
    if existing is None:
        session.add(StoreSetting(key=key, value=value))
    else:
        existing.value = value
    await session.commit()
    from conftest import bump_catalog

    await bump_catalog()  # direct SQL: what a settings change must trigger (cache + per-process copy)


async def add(client: AsyncClient, headers: dict[str, str], product_id: int, variant_id: int | None = None, quantity: int = 1) -> Any:
    body: dict[str, Any] = {"product_id": product_id, "quantity": quantity}
    if variant_id is not None:
        body["variant_id"] = variant_id
    return await client.post(f"{CART}/items", json=body, headers=headers)


async def cart(client: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    data: dict[str, Any] = ok(await client.get(CART, headers=headers))
    return data


def line_for(data: dict[str, Any], product_id: int, variant_id: int | None = None) -> dict[str, Any]:
    return next(l for l in data["lines"] if l["product"]["id"] == product_id and (l["variant"] or {}).get("id") == variant_id)


__all__ = ["ADMIN", "CART", "FAVORITES", "add", "build_shop", "cart", "line_for", "set_setting"]
