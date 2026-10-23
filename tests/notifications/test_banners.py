"""Public banners and the admin banner workflow."""

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from catalog_helpers import ADMIN, code_of, create_category, ok, png, published_product
from conftest import FakeClock
from notification_helpers import ADMIN_BANNERS, BANNERS

pytestmark = pytest.mark.anyio

NOON = datetime(2026, 7, 1, 10, 0)


async def create_banner(client: AsyncClient, headers: dict[str, str], title: str, **form: Any) -> Any:
    data = {"title": title, **{k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in form.items()}}
    return await client.post(ADMIN_BANNERS, data=data, files={"image": ("b.png", png(), "image/png")}, headers=headers)


def _image_files(media_root: Path) -> list[Path]:
    return [p for p in (media_root / "banners").rglob("*") if p.is_file()] if (media_root / "banners").exists() else []


async def test_public_banners_window_order_and_hidden_links(
    client: AsyncClient, admin_headers: dict, frozen_clock: FakeClock
) -> None:
    frozen_clock.now = NOON
    category = await create_category(client, admin_headers, "Banner Cat", slug="banner-cat")
    product = await published_product(client, admin_headers, name="Banner Cream", category_id=category["id"], price="5.00", stock=2)
    made = {}
    for title, form in (
        ("plain", {}),
        ("to product", {"link_type": "product", "link_value": product["slug"]}),
        ("to category", {"link_type": "category", "link_value": "banner-cat"}),
        ("off", {"is_active": False}),
        ("future", {"starts_at": "2026-07-02T00:00:00"}),
        ("past", {"starts_at": "2026-06-01T00:00:00", "ends_at": "2026-07-01T10:00:00"}),
        ("window", {"starts_at": "2026-06-01T00:00:00", "ends_at": "2026-07-01T10:00:01"}),
    ):
        made[title] = ok(await create_banner(client, admin_headers, title, **form), 201)
    assert made["plain"]["image"]["large"].endswith(".webp")

    public = ok(await client.get(BANNERS))
    assert [b["title"] for b in public] == ["plain", "to product", "to category", "window"]
    assert public[1]["link"] == {"type": "product", "value": product["slug"]}
    assert set(public[0]) == {"id", "title", "subtitle", "image", "link"}

    ok(await client.patch(f"{ADMIN}/products/{product['id']}", json={"is_active": False}, headers=admin_headers))
    assert [b["title"] for b in ok(await client.get(BANNERS))] == ["plain", "to category", "window"]

    states = {b["title"]: b["state"] for b in ok(await client.get(ADMIN_BANNERS, headers=admin_headers))}
    assert states == {"plain": "active", "to product": "active", "to category": "active", "off": "inactive",
                      "future": "scheduled", "past": "expired", "window": "active"}


async def test_at_most_ten_public_banners(client: AsyncClient, admin_headers: dict) -> None:
    for n in range(12):
        ok(await create_banner(client, admin_headers, f"b{n}"), 201)
    assert [b["title"] for b in ok(await client.get(BANNERS))] == [f"b{n}" for n in range(10)]


async def test_create_validation(client: AsyncClient, admin_headers: dict, customer_headers: dict, media_root: Path) -> None:
    no_image = await client.post(ADMIN_BANNERS, data={"title": "x"}, headers=admin_headers)
    assert no_image.status_code == 422
    assert code_of(await create_banner(client, admin_headers, "x", link_type="category", link_value="nope")) == "INVALID_LINK"
    assert code_of(await create_banner(client, admin_headers, "x", link_type="url", link_value="http://a.example")) == "INVALID_LINK"
    assert code_of(await create_banner(client, admin_headers, "x", link_type="admin_product", link_value="1")) == "INVALID_LINK"
    assert code_of(await create_banner(client, admin_headers, "x", link_type="new", link_value="x")) == "INVALID_LINK"
    window = await create_banner(client, admin_headers, "x", starts_at="2026-07-02T00:00:00", ends_at="2026-07-01T00:00:00")
    assert code_of(window) == "VALIDATION_ERROR"
    bad_image = await client.post(ADMIN_BANNERS, data={"title": "x"}, files={"image": ("b.png", b"not an image", "image/png")},
                                  headers=admin_headers)
    assert code_of(bad_image) == "INVALID_IMAGE"
    assert ok(await client.get(ADMIN_BANNERS, headers=admin_headers)) == []
    assert _image_files(media_root) == []  # nothing left behind
    assert (await create_banner(client, customer_headers, "x")).status_code == 403


async def test_update_image_delete_and_reorder(client: AsyncClient, admin_headers: dict, media_root: Path) -> None:
    a = ok(await create_banner(client, admin_headers, "a", subtitle="first"), 201)
    b = ok(await create_banner(client, admin_headers, "b"), 201)
    c = ok(await create_banner(client, admin_headers, "c"), 201)
    assert [x["sort_order"] for x in (a, b, c)] == [0, 1, 2]

    edited = ok(await client.patch(f"{ADMIN_BANNERS}/{a['id']}", json={"title": "A", "subtitle": None,
                                                                       "link": {"type": "sale"}}, headers=admin_headers))
    assert (edited["title"], edited["subtitle"], edited["link"]) == ("A", None, {"type": "sale", "value": None})
    assert code_of(await client.patch(f"{ADMIN_BANNERS}/{a['id']}", json={"link": {"type": "brand", "value": "nope"}},
                                      headers=admin_headers)) == "INVALID_LINK"
    assert code_of(await client.patch(f"{ADMIN_BANNERS}/{a['id']}", json={"starts_at": "2026-08-01T00:00:00",
                                                                          "ends_at": "2026-07-01T00:00:00"},
                                      headers=admin_headers)) == "VALIDATION_ERROR"

    old_files = set(_image_files(media_root))
    replaced = ok(await client.put(f"{ADMIN_BANNERS}/{a['id']}/image", files={"image": ("n.png", png((1, 2, 3)), "image/png")},
                                   headers=admin_headers))
    assert replaced["image"] != a["image"]
    new_files = set(_image_files(media_root))
    assert len(new_files) == len(old_files) and new_files != old_files  # the old sizes were removed after commit

    for wrong in ([a["id"], b["id"]], [a["id"], b["id"], c["id"], c["id"]], [a["id"], b["id"], 999999]):
        assert code_of(await client.put(f"{ADMIN_BANNERS}/order", json={"banner_ids": wrong}, headers=admin_headers)) == "BANNER_ORDER_MISMATCH"
    ordered = ok(await client.put(f"{ADMIN_BANNERS}/order", json={"banner_ids": [c["id"], a["id"], b["id"]]}, headers=admin_headers))
    assert [(x["title"], x["sort_order"]) for x in ordered] == [("c", 0), ("A", 1), ("b", 2)]
    assert [x["title"] for x in ok(await client.get(BANNERS))] == ["c", "A", "b"]

    before = len(_image_files(media_root))
    assert (await client.delete(f"{ADMIN_BANNERS}/{b['id']}", headers=admin_headers)).status_code == 204
    assert len(_image_files(media_root)) < before
    assert code_of(await client.get(f"{ADMIN_BANNERS}/{b['id']}", headers=admin_headers)) == "BANNER_NOT_FOUND"
    assert code_of(await client.delete(f"{ADMIN_BANNERS}/{b['id']}", headers=admin_headers)) == "BANNER_NOT_FOUND"
