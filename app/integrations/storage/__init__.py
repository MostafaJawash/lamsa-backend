from functools import lru_cache

from app.integrations.storage.base import StorageBackend

SUPPORTED_BACKENDS = ("local",)


@lru_cache
def get_storage() -> StorageBackend:
    """Return the storage backend selected by settings.STORAGE_BACKEND."""
    # Imported here so the pure modules in this package (images.py, local.py) can be
    # imported without a configured .env.
    from app.core.config import settings

    backend = settings.STORAGE_BACKEND.strip().lower()
    if backend == "local":
        from app.integrations.storage.local import LocalStorage

        return LocalStorage(root=settings.MEDIA_ROOT, base_url=settings.MEDIA_URL)
    raise RuntimeError(
        f"Unsupported STORAGE_BACKEND={settings.STORAGE_BACKEND!r}; "
        f"supported: {', '.join(SUPPORTED_BACKENDS)}"
    )


__all__ = ["StorageBackend", "get_storage"]
