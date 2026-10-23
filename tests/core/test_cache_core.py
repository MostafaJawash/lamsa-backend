"""The cache core over the in-memory backend: TTL, versions, schema, circuit breaker, single-flight."""

import asyncio
import logging

import pytest

from app.core import cache as cache_module
from app.core.cache import CACHE_SCHEMA_VERSION, Cache, MemoryBackend, jittered_ms, start_cache

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make() -> tuple[Cache, MemoryBackend, Clock]:
    clock = Clock()
    backend = MemoryBackend(now=clock)
    return Cache(backend, "t:"), backend, clock


async def test_set_get_ttl_and_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    cache, _, clock = make()
    monkeypatch.setattr(cache_module.random, "uniform", lambda a, b: 1.0)  # no jitter for this check
    await cache.set("k", b"v", 10)
    assert await cache.get("k") == b"v"
    clock.t += 9.9
    assert await cache.get("k") == b"v"
    clock.t += 0.2
    assert await cache.get("k") is None
    monkeypatch.undo()
    samples = {jittered_ms(100) for _ in range(200)}
    assert min(samples) >= 90_000 and max(samples) <= 110_000 and len(samples) > 1


async def test_versions_are_random_tokens_and_bumps_change_keys() -> None:
    cache, backend, _ = make()
    (first,) = await cache.versions(["catalog"])  # type: ignore[misc]
    assert len(first) == 12 and await cache.versions(["catalog"]) == [first]  # stable until bumped
    await cache.bump(["catalog"])
    (second,) = await cache.versions(["catalog"])  # type: ignore[misc]
    assert second != first
    assert cache.entry(first, "products", "x") != cache.entry(second, "products", "x")
    # An evicted version key is a fresh namespace, never an old number again.
    backend.data.pop("t:ver:catalog")
    (third,) = await cache.versions(["catalog"])  # type: ignore[misc]
    assert third not in (first, second)


def test_entry_key_format_and_schema_version() -> None:
    cache = Cache(MemoryBackend(), "lamsa:")
    assert cache.entry("abc123", "product", "lip") == f"c:{CACHE_SCHEMA_VERSION}:abc123:product:lip"
    assert cache.key(cache.entry("abc123", "categories")) == f"lamsa:c:{CACHE_SCHEMA_VERSION}:abc123:categories"
    other = Cache(MemoryBackend(), "lamsa:", schema=CACHE_SCHEMA_VERSION + "-next")
    assert other.entry("abc123", "product", "lip") != cache.entry("abc123", "product", "lip")


async def test_circuit_breaker_bypasses_and_warns_once(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    cache, backend, _ = make()
    now = [100.0]
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: now[0])
    backend.fail = True
    calls = 0

    async def compute() -> bytes:
        nonlocal calls
        calls += 1
        return b"fresh"

    with caplog.at_level(logging.WARNING, logger="app.cache"):
        assert await cache.get_or_compute("k", 10, compute) == b"fresh"
        assert await cache.versions(["catalog"]) is None
        backend_calls = backend.calls
        for _ in range(5):  # circuit open: the backend is not even called
            assert await cache.get_or_compute("k", 10, compute) == b"fresh"
        assert backend.calls == backend_calls
        now[0] += cache_module.CIRCUIT_OPEN_S + 1  # half-open: tries again, fails again
        assert await cache.get("k") is None
    assert calls == 6
    assert len([r for r in caplog.records if "Cache unavailable" in r.message]) == 1  # at most once a minute
    backend.fail = False
    now[0] += cache_module.CIRCUIT_OPEN_S + 1
    assert await cache.get_or_compute("k", 10, compute) == b"fresh" and await cache.get("k") == b"fresh"


async def test_single_flight_computes_once_per_key() -> None:
    cache, _, _ = make()
    calls = 0

    async def compute() -> bytes:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return b"value"

    results = await asyncio.gather(*[cache.get_or_compute("hot", 30, compute) for _ in range(25)])
    assert results == [b"value"] * 25 and calls == 1


async def test_another_worker_holding_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second process computes the value: we wait for it; if it never comes, we compute."""
    cache, backend, _ = make()
    monkeypatch.setattr(cache_module, "LOCK_WAIT_S", 0.2)
    await backend.set_nx("t:slow:lock", b"other-worker", 3000)

    async def fill_later() -> None:
        await asyncio.sleep(0.05)
        await backend.set("t:slow", b"from-other-worker", 10_000)

    async def compute() -> bytes:
        return b"ours"

    asyncio.get_running_loop().create_task(fill_later())
    assert await cache.get_or_compute("slow", 10, compute) == b"from-other-worker"
    await backend.set_nx("t:stuck:lock", b"other-worker", 3000)  # holder never finishes
    assert await cache.get_or_compute("stuck", 10, compute) == b"ours"


async def test_startup_bumps_the_catalog_version(memory_cache: Cache) -> None:
    (before,) = await memory_cache.versions(["catalog"])  # type: ignore[misc]
    await start_cache()
    (after,) = await memory_cache.versions(["catalog"])  # type: ignore[misc]
    assert before != after


async def test_disabled_cache_computes_every_time() -> None:
    cache = Cache(None)
    calls = 0

    async def compute() -> bytes:
        nonlocal calls
        calls += 1
        return b"x"

    assert await cache.versions(["catalog"]) is None
    for _ in range(3):
        assert await cache.get_or_compute("k", 10, compute) == b"x"
    assert calls == 3


async def test_one_slow_call_does_not_open_the_circuit() -> None:
    cache, backend, _ = make()
    await cache.set("k", b"v", 60)
    backend.fail = True
    for _ in range(cache_module.FAILURES_TO_OPEN - 1):
        assert await cache.get("k") is None
    backend.fail = False
    assert cache.available and await cache.get("k") == b"v"  # a success resets the count
    backend.fail = True
    for _ in range(cache_module.FAILURES_TO_OPEN):
        await cache.get("k")
    assert not cache.available
