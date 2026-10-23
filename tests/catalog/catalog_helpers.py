"""Helpers for catalog tests: build data through the real admin API."""

from io import BytesIO
from typing import Any

from httpx import AsyncClient, Response
from PIL import Image

ADMIN = "/api/v1/admin"
PUBLIC = "/api/v1"


def png(color: tuple[int, int, int] = (200, 120, 140), size: tuple[int, int] = (64, 64)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def ok(response: Response, status: int = 200) -> Any:
    assert response.status_code == status, f"{response.status_code}: {response.text}"
    return response.json() if response.content else None


def code_of(response: Response) -> str:
    body = response.json()
    assert set(body) == {"error"}, body
    code: str = body["error"]["code"]
    return code


async def create_brand(client: AsyncClient, headers: dict[str, str], name: str, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = ok(await client.post(f"{ADMIN}/brands", json={"name": name, **extra}, headers=headers), 201)
    return data


async def create_category(client: AsyncClient, headers: dict[str, str], name: str, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = ok(
        await client.post(f"{ADMIN}/categories", json={"name": name, **extra}, headers=headers), 201
    )
    return data


async def add_attribute(
    client: AsyncClient, headers: dict[str, str], category_id: int, **body: Any
) -> dict[str, Any]:
    data: dict[str, Any] = ok(
        await client.post(f"{ADMIN}/categories/{category_id}/attributes", json=body, headers=headers), 201
    )
    return data


async def add_variant_option(
    client: AsyncClient, headers: dict[str, str], category_id: int, **body: Any
) -> dict[str, Any]:
    data: dict[str, Any] = ok(
        await client.post(f"{ADMIN}/categories/{category_id}/variant-options", json=body, headers=headers), 201
    )
    return data


async def create_product(client: AsyncClient, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    body.setdefault("type", "simple")
    if body["type"] == "simple":
        body.setdefault("price", "10.00")
        body.setdefault("stock", 10)
    if body["type"] == "bundle":
        body.setdefault("price", "20.00")
    data: dict[str, Any] = ok(await client.post(f"{ADMIN}/products", json=body, headers=headers), 201)
    return data


async def upload_images(
    client: AsyncClient,
    headers: dict[str, str],
    product_id: int,
    count: int = 1,
    *,
    variant_id: int | None = None,
    color: str | None = None,
    payloads: list[bytes] | None = None,
) -> Response:
    files = [("files", (f"img{i}.png", data, "image/png")) for i, data in enumerate(payloads or [png() for _ in range(count)])]
    form: dict[str, str] = {}
    if variant_id is not None:
        form["variant_id"] = str(variant_id)
    if color is not None:
        form["color"] = color
    return await client.post(f"{ADMIN}/products/{product_id}/images", files=files, data=form, headers=headers)


async def publish(client: AsyncClient, headers: dict[str, str], product_id: int) -> dict[str, Any]:
    data: dict[str, Any] = ok(
        await client.patch(f"{ADMIN}/products/{product_id}", json={"is_active": True}, headers=headers)
    )
    return data


async def published_product(client: AsyncClient, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    """Create, add one image, publish."""
    product = await create_product(client, headers, **body)
    ok(await upload_images(client, headers, product["id"]), 201)
    return await publish(client, headers, product["id"])


async def admin_product(client: AsyncClient, headers: dict[str, str], product_id: int) -> dict[str, Any]:
    data: dict[str, Any] = ok(await client.get(f"{ADMIN}/products/{product_id}", headers=headers))
    return data


def variant(color: str | None = None, hex_value: str | None = None, *, price: str = "10.00", stock: int = 5, **extra: Any) -> dict[str, Any]:
    options: dict[str, str] = dict(extra.pop("options", {}))
    if color is not None:
        options["color"] = color
        options["color_hex"] = hex_value or "#aa0000"
    return {"options": options, "price": price, "stock": stock, **extra}
