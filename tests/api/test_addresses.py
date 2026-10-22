import pytest
from httpx import AsyncClient

from app.modules.users.service import MAX_ADDRESSES
from conftest import FakeClock, bearer, error_code, register

pytestmark = pytest.mark.anyio

BASE = "/api/v1/users/me/addresses"


async def user_headers(client: AsyncClient, n: int) -> dict[str, str]:
    return bearer((await register(client, n))["access_token"])


async def add(client: AsyncClient, headers: dict[str, str], street: str, **extra: object) -> dict:
    response = await client.post(BASE, json={"city": "Berlin", "street": street, **extra}, headers=headers)
    assert response.status_code == 201, response.text
    data: dict = response.json()
    return data


async def listing(client: AsyncClient, headers: dict[str, str]) -> list[dict]:
    data: list[dict] = (await client.get(BASE, headers=headers)).json()
    return data


async def test_first_address_is_default_and_strings_trimmed(client: AsyncClient) -> None:
    headers = await user_headers(client, 81)
    first = await add(client, headers, "  Hauptstr. 1  ", label="  البيت ", building="   ", notes="")
    assert first["is_default"] is True
    assert first["street"] == "Hauptstr. 1"
    assert first["label"] == "البيت"
    assert first["building"] is None and first["notes"] is None


async def test_setting_default_unsets_others_and_order(client: AsyncClient, frozen_clock: FakeClock) -> None:
    headers = await user_headers(client, 82)
    a = await add(client, headers, "A")
    frozen_clock.advance(seconds=10)
    b = await add(client, headers, "B")
    frozen_clock.advance(seconds=10)
    c = await add(client, headers, "C", is_default=True)

    items = await listing(client, headers)
    assert [i["street"] for i in items] == ["C", "B", "A"]  # default first, then newest
    assert [i["is_default"] for i in items] == [True, False, False]

    response = await client.post(f"{BASE}/{a['id']}/default", headers=headers)
    assert response.status_code == 200 and response.json()["is_default"] is True
    items = await listing(client, headers)
    assert [i["street"] for i in items] == ["A", "C", "B"]
    assert sum(i["is_default"] for i in items) == 1

    patched = await client.patch(f"{BASE}/{b['id']}", json={"is_default": True, "notes": "الطابق ٢"}, headers=headers)
    assert patched.json()["is_default"] is True and patched.json()["notes"] == "الطابق ٢"
    assert sum(i["is_default"] for i in await listing(client, headers)) == 1
    assert c["id"]


async def test_deleting_default_promotes_newest(client: AsyncClient, frozen_clock: FakeClock) -> None:
    headers = await user_headers(client, 83)
    a = await add(client, headers, "A")
    frozen_clock.advance(seconds=10)
    await add(client, headers, "B")
    frozen_clock.advance(seconds=10)
    await add(client, headers, "C")

    assert (await client.delete(f"{BASE}/{a['id']}", headers=headers)).status_code == 204
    items = await listing(client, headers)
    assert [(i["street"], i["is_default"]) for i in items] == [("C", True), ("B", False)]


async def test_deleting_last_address(client: AsyncClient) -> None:
    headers = await user_headers(client, 84)
    a = await add(client, headers, "A")
    assert (await client.delete(f"{BASE}/{a['id']}", headers=headers)).status_code == 204
    assert await listing(client, headers) == []


async def test_address_limit(client: AsyncClient) -> None:
    headers = await user_headers(client, 85)
    for i in range(MAX_ADDRESSES):
        await add(client, headers, f"Street {i}")
    response = await client.post(BASE, json={"city": "Berlin", "street": "One too many"}, headers=headers)
    assert response.status_code == 422
    assert error_code(response) == "ADDRESS_LIMIT_REACHED"


async def test_other_users_address_is_not_found(client: AsyncClient) -> None:
    owner = await user_headers(client, 86)
    other = await user_headers(client, 87)
    address = await add(client, owner, "Private")

    for method, path, body in (
        ("PATCH", f"{BASE}/{address['id']}", {"city": "Hacked"}),
        ("DELETE", f"{BASE}/{address['id']}", None),
        ("POST", f"{BASE}/{address['id']}/default", None),
    ):
        response = await client.request(method, path, json=body, headers=other)
        assert response.status_code == 404, path
        assert error_code(response) == "ADDRESS_NOT_FOUND"
    assert (await listing(client, owner))[0]["city"] == "Berlin"


async def test_address_validation(client: AsyncClient) -> None:
    headers = await user_headers(client, 88)
    for body in (
        {"city": "Berlin"},
        {"city": "   ", "street": "A"},
        {"city": "Berlin", "street": "x" * 256},
        {"city": "Berlin", "street": "A", "notes": "n" * 501},
    ):
        response = await client.post(BASE, json=body, headers=headers)
        assert response.status_code == 422, body
        assert error_code(response) == "VALIDATION_ERROR"
        assert response.json()["error"]["details"][0]["field"] in {"street", "city", "notes"}

    a = await add(client, headers, "A")
    unchanged = await client.patch(f"{BASE}/{a['id']}", json={"city": None, "label": "الشغل"}, headers=headers)
    assert unchanged.json()["city"] == "Berlin" and unchanged.json()["label"] == "الشغل"
