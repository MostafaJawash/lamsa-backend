from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.integrations.storage.images import ImageSize
from app.integrations.storage.local import LocalStorage
from app.integrations.storage.service import ImageService

pytestmark = pytest.mark.anyio

MAX_BYTES = 5 * 1024 * 1024


def jpeg_bytes(size: tuple[int, int] = (1200, 900)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()


class FailingStorage(LocalStorage):
    """Fails on the n-th save to exercise cleanup."""

    def __init__(self, root: Path, fail_on: int) -> None:
        super().__init__(root=root, base_url="http://test/media")
        self.fail_on = fail_on
        self.saves = 0

    async def save(self, key: str, data: bytes, content_type: str) -> None:
        self.saves += 1
        if self.saves == self.fail_on:
            raise OSError("disk full")
        await super().save(key, data, content_type)


async def test_save_stores_all_sizes(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path, base_url="http://test/media")
    service = ImageService(storage, MAX_BYTES)

    saved = await service.save("products/42", jpeg_bytes())

    assert saved.image_key.startswith("products/42/")
    assert (saved.width, saved.height) == (1200, 900)
    for size in ImageSize:
        assert await storage.exists(f"{saved.image_key}_{size}.webp")
    assert service.url(saved.image_key) == f"http://test/media/{saved.image_key}_medium.webp"
    assert set(service.urls(saved.image_key)) == {"thumb", "medium", "large"}


async def test_delete_removes_all_sizes(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path, base_url="http://test/media")
    service = ImageService(storage, MAX_BYTES)
    saved = await service.save("banners", jpeg_bytes())

    await service.delete(saved.image_key)

    assert list((tmp_path / "banners").iterdir()) == []


async def test_partial_failure_cleans_up_and_reraises(tmp_path: Path) -> None:
    storage = FailingStorage(tmp_path, fail_on=3)
    service = ImageService(storage, MAX_BYTES)

    with pytest.raises(OSError, match="disk full"):
        await service.save("products/1", jpeg_bytes())

    assert list((tmp_path / "products" / "1").iterdir()) == []
