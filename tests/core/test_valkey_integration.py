"""Against a REAL Valkey (docker compose up -d valkey); skipped when it is not reachable.

Uses database 15 and a unique key prefix per test, and deletes its keys afterwards.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest

from app.core.cache import Cache, ValkeyBackend
from app.core.config import settings

pytestmark = pytest.mark.anyio


@pytest.fixture
async def valkey() -> AsyncIterator[Cache]:
    backend = ValkeyBackend(settings.CACHE_URL, timeout_ms=300)
    try:
        await backend.ping()
    except Exception:
        await backend.close()
        pytest.skip(f"Valkey not reachable at {settings.CACHE_URL}")
    prefix = f"lamsa-it:{uuid.uuid4().hex[:8]}:"
    cache = Cache(backend, prefix)
    try:
        yield cache
    finally:
        keys = [k async for k in backend.client.scan_iter(match=f"{prefix}*")]
        if keys:
            await backend.client.delete(*keys)
        await backend.close()


async def test_get_set_ttl_and_versions(valkey: Cache) -> None:
    await valkey.set("k", b"value", 0.3)
    assert await valkey.get("k") == b"value"
    await asyncio.sleep(0.45)
    assert await valkey.get("k") is None
    (first,) = await valkey.versions(["catalog"])  # type: ignore[misc]
    await valkey.bump(["catalog"])
    (second,) = await valkey.versions(["catalog"])  # type: ignore[misc]
    assert first != second


async def test_single_flight_with_the_real_lock(valkey: Cache) -> None:
    calls = 0

    async def compute() -> bytes:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return b"once"

    other_worker = Cache(ValkeyBackend(settings.CACHE_URL, timeout_ms=300), valkey.prefix)  # same keys, own locks
    try:
        results = await asyncio.gather(*[c.get_or_compute("hot", 30, compute) for c in [valkey, other_worker] * 10])
    finally:
        await other_worker.close()
    assert results == [b"once"] * 20 and calls == 1


async def test_unreachable_server_is_bypassed_quickly() -> None:
    cache = Cache(ValkeyBackend("redis://127.0.0.1:1/0", timeout_ms=150), "x:")
    calls = 0

    async def compute() -> bytes:
        nonlocal calls
        calls += 1
        return b"computed"

    loop = asyncio.get_running_loop()
    start = loop.time()
    for _ in range(20):
        assert await cache.get_or_compute("k", 10, compute) == b"computed"
    assert calls == 20 and loop.time() - start < 1.0  # circuit open after the first failure
    await cache.close()
