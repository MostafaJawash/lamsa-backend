from typing import Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    """Where media files live. Keys are relative, "/"-separated paths such as
    "products/42/3f9c..._medium.webp". Implementations: LocalStorage (an S3 backend can be
    added later without touching callers).
    """

    async def save(self, key: str, data: bytes, content_type: str) -> None:
        """Store data under key, replacing any existing object."""
        ...

    async def delete(self, keys: list[str]) -> None:
        """Delete the given keys; missing keys are ignored."""
        ...

    async def exists(self, key: str) -> bool: ...

    async def check(self) -> bool:
        """True if the backend is reachable and writable (used by /health)."""
        ...

    def url(self, key: str) -> str:
        """Public URL for key."""
        ...
