from pathlib import Path

import pytest

from app.integrations.storage.base import StorageBackend
from app.integrations.storage.local import LocalStorage
from app.shared.exceptions import InvalidStorageKeyError

pytestmark = pytest.mark.anyio


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(root=tmp_path / "media", base_url="http://127.0.0.1:8000/media/")


def test_implements_protocol(storage: LocalStorage) -> None:
    assert isinstance(storage, StorageBackend)


async def test_save_creates_folders_and_writes_file(storage: LocalStorage) -> None:
    await storage.save("products/42/abc_large.webp", b"data", "image/webp")

    path = storage.root / "products" / "42" / "abc_large.webp"
    assert path.read_bytes() == b"data"
    assert await storage.exists("products/42/abc_large.webp")


async def test_save_overwrites_and_leaves_no_temp_files(storage: LocalStorage) -> None:
    await storage.save("banners/a.webp", b"old", "image/webp")
    await storage.save("banners/a.webp", b"new", "image/webp")

    folder = storage.root / "banners"
    assert (folder / "a.webp").read_bytes() == b"new"
    assert [p.name for p in folder.iterdir()] == ["a.webp"]


async def test_delete_removes_files_and_ignores_missing(storage: LocalStorage) -> None:
    await storage.save("brands/7/a.webp", b"1", "image/webp")
    await storage.save("brands/7/b.webp", b"2", "image/webp")

    await storage.delete(["brands/7/a.webp", "brands/7/b.webp", "brands/7/missing.webp"])

    assert not await storage.exists("brands/7/a.webp")
    assert not await storage.exists("brands/7/b.webp")


@pytest.mark.parametrize(
    "key",
    [
        "",
        "../outside.webp",
        "products/../../outside.webp",
        "products/..",
        "/etc/passwd",
        "C:/Windows/evil.webp",
        "C:evil.webp",
        "\\\\server\\share\\evil.webp",
        "products\\..\\..\\evil.webp",
    ],
)
async def test_path_traversal_is_rejected(storage: LocalStorage, key: str) -> None:
    with pytest.raises(InvalidStorageKeyError):
        await storage.save(key, b"x", "image/webp")
    with pytest.raises(InvalidStorageKeyError):
        await storage.delete([key])
    with pytest.raises(InvalidStorageKeyError):
        storage.url(key)


async def test_nothing_written_outside_root(storage: LocalStorage, tmp_path: Path) -> None:
    with pytest.raises(InvalidStorageKeyError):
        await storage.save("../escaped.webp", b"x", "image/webp")
    assert not (tmp_path / "escaped.webp").exists()


def test_url_building(storage: LocalStorage) -> None:
    assert storage.url("products/42/abc_medium.webp") == (
        "http://127.0.0.1:8000/media/products/42/abc_medium.webp"
    )


async def test_check_reports_missing_and_writable_root(storage: LocalStorage) -> None:
    assert not await storage.check()  # root not created yet
    storage.root.mkdir(parents=True)
    assert await storage.check()
