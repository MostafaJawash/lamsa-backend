"""Helpers for order tests."""

from decimal import Decimal
from typing import Any

from httpx import AsyncClient, Response

from cart_fixtures import CART, add

ORDERS = "/api/v1/orders"
ADMIN_ORDERS = "/api/v1/admin/orders"


async def address(client: AsyncClient, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/users/me/addresses",
        json={"label": "البيت", "city": "Berlin", "street": "Hauptstr. 1", "building": "3. OG", "notes": "Klingel 2", **body},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    return data


async def expected_total(client: AsyncClient, headers: dict[str, str], method: str) -> str:
    """What the checkout screen shows: cart subtotal + the method's quote (discount 0)."""
    cart = (await client.get(CART, headers=headers)).json()
    quote = next(q for q in cart["delivery_quotes"] if q["code"] == method)
    return f"{Decimal(cart['subtotal']) + Decimal(quote['fee']):.2f}"


async def place(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    method: str = "home",
    address_id: int | None = None,
    total: str | None = None,
    key: str | None = None,
    **extra: Any,
) -> Response:
    body: dict[str, Any] = {"delivery_method_code": method, **extra}
    if address_id is not None:
        body["address_id"] = address_id
    body["expected_total"] = total if total is not None else await expected_total(client, headers, method)
    request_headers = dict(headers)
    if key is not None:
        request_headers["Idempotency-Key"] = key
    return await client.post(ORDERS, json=body, headers=request_headers)


async def order_from_cart(
    client: AsyncClient, headers: dict[str, str], items: list[tuple[int, int | None, int]], *, method: str = "home",
    address_id: int | None = None,
) -> dict[str, Any]:
    """Fill the cart with (product_id, variant_id, quantity) and place the order."""
    for product_id, variant_id, quantity in items:
        response = await add(client, headers, product_id, variant_id, quantity)
        assert response.status_code in (200, 201), response.text
    if method != "pickup" and address_id is None:
        address_id = (await address(client, headers))["id"]
    response = await place(client, headers, method=method, address_id=address_id)
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    return data
