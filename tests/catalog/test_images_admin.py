import os
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from catalog_helpers import ADMIN, add_variant_option, code_of, create_category, create_product, ok, png, publish, upload_images, variant

pytestmark = pytest.mark.anyio


@pytest.fixture
async def product(client: AsyncClient, admin_headers: dict[str, str]) -> dict[str, Any]:
    category = await create_category(client, admin_headers, "Imgs")
    await add_variant_option(client, admin_headers, category["id"], key="color", label="اللون", type="color")
    return await create_product(client, admin_headers, type="variable", name="Lip", category_id=category["id"],
                                variants=[variant("أحمر", "#FF0000"), variant("أزرق", "#0000FF")])


def files_in(media_root: Path, product_id: int) -> list[str]:
    folder = media_root / "products" / str(product_id)
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


async def test_upload_multiple_and_order(client: AsyncClient, admin_headers: dict[str, str], product: dict, media_root: Path) -> None:
    first = ok(await upload_images(client, admin_headers, product["id"], 3), 201)["images"]
    assert [i["sort_order"] for i in first] == [0, 1, 2]
    assert all(i["urls"]["large"].endswith("_large.webp") and i["width"] == 64 for i in first)
    assert len(files_in(media_root, product["id"])) == 9  # 3 images x 3 sizes
    second = ok(await upload_images(client, admin_headers, product["id"], 1), 201)["images"]
    assert second[0]["sort_order"] == 3

    ids = [i["id"] for i in first + second]
    reordered = ok(await client.put(f"{ADMIN}/products/{product['id']}/images/order", json={"image_ids": ids[::-1]}, headers=admin_headers))
    assert [i["id"] for i in reordered] == ids[::-1] and [i["sort_order"] for i in reordered] == [0, 1, 2, 3]
    for bad in (ids[:-1], [*ids, ids[0]], [*ids[:-1], 999_999]):
        response = await client.put(f"{ADMIN}/products/{product['id']}/images/order", json={"image_ids": bad}, headers=admin_headers)
        assert response.status_code == 422 and code_of(response) == "IMAGE_ORDER_MISMATCH", bad


async def test_limits_and_invalid_files(client: AsyncClient, admin_headers: dict[str, str], product: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    too_many_files = await upload_images(client, admin_headers, product["id"], 9)
    assert too_many_files.status_code == 422 and code_of(too_many_files) == "VALIDATION_ERROR"
    ok(await upload_images(client, admin_headers, product["id"], 8), 201)
    ok(await upload_images(client, admin_headers, product["id"], 7), 201)
    limit = await upload_images(client, admin_headers, product["id"], 1)
    assert limit.status_code == 422 and code_of(limit) == "IMAGE_LIMIT_REACHED"

    other = await create_product(client, admin_headers, name="Other", category_id=product["category"]["id"])
    invalid = await upload_images(client, admin_headers, other["id"], payloads=[b"not an image at all"])
    assert invalid.status_code == 422 and code_of(invalid) == "INVALID_IMAGE"

    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 1)
    too_big = await upload_images(client, admin_headers, other["id"], payloads=[os.urandom(1024 * 1024 + 10)])
    assert too_big.status_code == 413 and code_of(too_big) == "FILE_TOO_LARGE"
    huge_body = await upload_images(client, admin_headers, other["id"], payloads=[os.urandom(10 * 1024 * 1024)])
    assert huge_body.status_code == 413 and code_of(huge_body) == "FILE_TOO_LARGE"  # Content-Length guard


async def test_patch_and_delete(client: AsyncClient, admin_headers: dict[str, str], product: dict, media_root: Path) -> None:
    red, blue = product["variants"]
    image = ok(await upload_images(client, admin_headers, product["id"]), 201)["images"][0]
    url = f"{ADMIN}/products/{product['id']}/images/{image['id']}"
    patched = ok(await client.patch(url, json={"alt_text": "  أحمر شفاه  ", "variant_id": blue["id"]}, headers=admin_headers))
    assert (patched["alt_text"], patched["variant_id"], patched["color"]) == ("أحمر شفاه", blue["id"], "أزرق")
    other = await create_product(client, admin_headers, type="variable", name="O", category_id=product["category"]["id"], variants=[variant("أحمر")])
    foreign = await client.patch(url, json={"variant_id": other["variants"][0]["id"]}, headers=admin_headers)
    assert foreign.status_code == 404 and code_of(foreign) == "VARIANT_NOT_FOUND"
    general = ok(await client.patch(url, json={"variant_id": None}, headers=admin_headers))
    assert general["variant_id"] is None and general["color"] is None

    await publish(client, admin_headers, product["id"])
    deleted = ok(await client.delete(url, headers=admin_headers))
    assert deleted == {"deleted": True, "warning": "LAST_IMAGE_OF_ACTIVE_PRODUCT"}
    assert files_in(media_root, product["id"]) == []
    missing = await client.delete(url, headers=admin_headers)
    assert missing.status_code == 404 and code_of(missing) == "IMAGE_NOT_FOUND"


async def test_files_follow_the_transaction(
    client: AsyncClient, admin_headers: dict[str, str], product: dict, media_root: Path, db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kept = ok(await upload_images(client, admin_headers, product["id"]), 201)["images"][0]
    before = files_in(media_root, product["id"])
    assert len(before) == 3

    async def failing_commit() -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(db_session, "commit", failing_commit)
    # Upload whose commit fails: the new files are removed again.
    failed_upload = await upload_images(client, admin_headers, product["id"], 2)
    assert failed_upload.status_code == 500 and code_of(failed_upload) == "INTERNAL_ERROR"
    assert files_in(media_root, product["id"]) == before
    # Delete whose commit fails: the files stay (deletion happens only after commit).
    failed_delete = await client.delete(f"{ADMIN}/products/{product['id']}/images/{kept['id']}", headers=admin_headers)
    assert failed_delete.status_code == 500
    assert files_in(media_root, product["id"]) == before

    monkeypatch.undo()
    ok(await client.delete(f"{ADMIN}/products/{product['id']}/images/{kept['id']}", headers=admin_headers))
    assert files_in(media_root, product["id"]) == []


async def test_product_delete_removes_files_after_commit(
    client: AsyncClient, admin_headers: dict[str, str], media_root: Path
) -> None:
    category = await create_category(client, admin_headers, "Del")
    product = await create_product(client, admin_headers, name="Gone", category_id=category["id"])
    ok(await upload_images(client, admin_headers, product["id"], 2), 201)
    assert len(files_in(media_root, product["id"])) == 6
    assert (await client.delete(f"{ADMIN}/products/{product['id']}", headers=admin_headers)).status_code == 204
    assert files_in(media_root, product["id"]) == []
    assert png
