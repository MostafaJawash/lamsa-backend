from decimal import Decimal

import pytest
from httpx import AsyncClient

from app.modules.users.service import MAX_ADDRESSES
from conftest import FakeClock, bearer, error_code, register

pytestmark = pytest.mark.anyio
BASE = "/api/v1/users/me/addresses"


async def user_headers(client: AsyncClient, n: int) -> dict[str, str]:
    return bearer((await register(client, n))["access_token"])


async def add(client: AsyncClient, headers: dict[str, str], details: str, **extra: object) -> dict:
    response = await client.post(
        BASE,
        json={"governorate": "damascus", "details": details, **extra},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def listing(client: AsyncClient, headers: dict[str, str]) -> list[dict]:
    return (await client.get(BASE, headers=headers)).json()


async def test_governorates_are_public_and_include_all_syria(client: AsyncClient) -> None:
    response = await client.get("/api/v1/governorates")
    assert response.status_code == 200
    items = response.json()
    assert len(items) == 14
    assert {item["code"] for item in items} >= {"damascus", "rif_dimashq", "aleppo"}
    assert next(item for item in items if item["code"] == "rif_dimashq")["name"] == "ريف دمشق"


async def test_first_address_is_default_and_strings_trimmed(client: AsyncClient) -> None:
    headers = await user_headers(client, 81)
    first = await add(
        client,
        headers,
        "  شارع بغداد، بناء ١٢  ",
        label="  البيت ",
        area="   ",
        notes="",
        latitude="33.513800",
        longitude="36.276500",
    )
    assert first["is_default"] is True
    assert first["details"] == "شارع بغداد، بناء ١٢"
    assert first["label"] == "البيت"
    assert first["area"] is None and first["notes"] is None
    assert Decimal(first["latitude"]) == Decimal("33.513800")


async def test_setting_default_unsets_others_and_order(client: AsyncClient, frozen_clock: FakeClock) -> None:
    headers = await user_headers(client, 82)
    a = await add(client, headers, "العنوان الأول في دمشق")
    frozen_clock.advance(seconds=10)
    b = await add(client, headers, "العنوان الثاني في دمشق")
    frozen_clock.advance(seconds=10)
    c = await add(client, headers, "العنوان الثالث في دمشق", is_default=True)

    items = await listing(client, headers)
    assert [i["details"] for i in items] == [
        "العنوان الثالث في دمشق", "العنوان الثاني في دمشق", "العنوان الأول في دمشق"
    ]
    assert [i["is_default"] for i in items] == [True, False, False]

    response = await client.post(f"{BASE}/{a['id']}/default", headers=headers)
    assert response.status_code == 200 and response.json()["is_default"] is True
    items = await listing(client, headers)
    assert [i["details"] for i in items] == [
        "العنوان الأول في دمشق", "العنوان الثالث في دمشق", "العنوان الثاني في دمشق"
    ]
    assert sum(i["is_default"] for i in items) == 1

    patched = await client.patch(
        f"{BASE}/{b['id']}", json={"is_default": True, "notes": "الطابق ٢"}, headers=headers
    )
    assert patched.json()["is_default"] is True and patched.json()["notes"] == "الطابق ٢"
    assert sum(i["is_default"] for i in await listing(client, headers)) == 1
    assert c["id"]


async def test_deleting_default_promotes_newest(client: AsyncClient, frozen_clock: FakeClock) -> None:
    headers = await user_headers(client, 83)
    a = await add(client, headers, "العنوان الأول في دمشق")
    frozen_clock.advance(seconds=10)
    await add(client, headers, "العنوان الثاني في دمشق")
    frozen_clock.advance(seconds=10)
    await add(client, headers, "العنوان الثالث في دمشق")
    assert (await client.delete(f"{BASE}/{a['id']}", headers=headers)).status_code == 204
    assert [(i["details"], i["is_default"]) for i in await listing(client, headers)] == [
        ("العنوان الثالث في دمشق", True), ("العنوان الثاني في دمشق", False)
    ]


async def test_deleting_last_address(client: AsyncClient) -> None:
    headers = await user_headers(client, 84)
    a = await add(client, headers, "العنوان الوحيد في دمشق")
    assert (await client.delete(f"{BASE}/{a['id']}", headers=headers)).status_code == 204
    assert await listing(client, headers) == []


async def test_address_limit(client: AsyncClient) -> None:
    headers = await user_headers(client, 85)
    for i in range(MAX_ADDRESSES):
        await add(client, headers, f"عنوان صالح رقم {i:02d}")
    response = await client.post(
        BASE,
        json={"governorate": "damascus", "details": "عنوان زائد عن الحد"},
        headers=headers,
    )
    assert response.status_code == 422
    assert error_code(response) == "ADDRESS_LIMIT_REACHED"


async def test_other_users_address_is_not_found(client: AsyncClient) -> None:
    owner = await user_headers(client, 86)
    other = await user_headers(client, 87)
    address = await add(client, owner, "عنوان خاص في دمشق")
    for method, path, body in (
        ("PATCH", f"{BASE}/{address['id']}", {"governorate": "aleppo"}),
        ("DELETE", f"{BASE}/{address['id']}", None),
        ("POST", f"{BASE}/{address['id']}/default", None),
    ):
        response = await client.request(method, path, json=body, headers=other)
        assert response.status_code == 404, path
        assert error_code(response) == "ADDRESS_NOT_FOUND"
    assert (await listing(client, owner))[0]["governorate"] == "damascus"


async def test_address_validation(client: AsyncClient) -> None:
    headers = await user_headers(client, 88)
    for body, field in (
        ({"governorate": "damascus"}, "details"),
        ({"governorate": "unknown", "details": "عنوان صالح في دمشق"}, "governorate"),
        ({"governorate": "damascus", "details": "قصير"}, "details"),
        ({"governorate": "damascus", "details": "x" * 501}, "details"),
        ({"governorate": "damascus", "details": "عنوان صالح في دمشق", "notes": "n" * 501}, "notes"),
    ):
        response = await client.post(BASE, json=body, headers=headers)
        assert response.status_code == 422, body
        assert error_code(response) == "VALIDATION_ERROR"
        assert response.json()["error"]["details"][0]["field"] == field

    a = await add(client, headers, "عنوان صالح في دمشق")
    unchanged = await client.patch(
        f"{BASE}/{a['id']}",
        json={"governorate": None, "details": None, "label": "الشغل"},
        headers=headers,
    )
    assert unchanged.json()["governorate"] == "damascus"
    assert unchanged.json()["details"] == "عنوان صالح في دمشق"
    assert unchanged.json()["label"] == "الشغل"


@pytest.mark.parametrize(
    "coordinates",
    [
        {"latitude": "33.5"},
        {"longitude": "36.3"},
        {"latitude": "31.9", "longitude": "36.3"},
        {"latitude": "33.5", "longitude": "43.0"},
        {"latitude": "33.1234567", "longitude": "36.3"},
    ],
)
async def test_invalid_location_is_rejected(client: AsyncClient, coordinates: dict[str, str]) -> None:
    headers = await user_headers(client, 89)
    response = await client.post(
        BASE,
        json={"governorate": "damascus", "details": "عنوان صالح في دمشق", **coordinates},
        headers=headers,
    )
    assert response.status_code == 422
    assert error_code(response) == "INVALID_LOCATION"


async def test_clearing_one_coordinate_requires_clearing_both(client: AsyncClient) -> None:
    headers = await user_headers(client, 90)
    address = await add(
        client, headers, "عنوان صالح في دمشق", latitude="33.5138", longitude="36.2765"
    )
    rejected = await client.patch(
        f"{BASE}/{address['id']}", json={"latitude": None}, headers=headers
    )
    assert rejected.status_code == 422
    assert error_code(rejected) == "INVALID_LOCATION"
    cleared = await client.patch(
        f"{BASE}/{address['id']}",
        json={"latitude": None, "longitude": None},
        headers=headers,
    )
    assert cleared.status_code == 200
    assert cleared.json()["latitude"] is None and cleared.json()["longitude"] is None
