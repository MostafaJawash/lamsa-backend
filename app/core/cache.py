"""The application cache (Valkey, redis protocol): small, optional, never a source of truth.

What is cached is decided by the endpoints (see app/shared/http_cache.py and the README). This
module only offers:

- get / set with TTL (plus ±10 % jitter), delete;
- versions: random tokens per name (``catalog``, ``product:<slug>``) that are part of the entry
  keys. A bump writes a new random token, so every old entry is simply never read again (no key
  scanning). Random tokens, not counters: under allkeys-lru a version key may be evicted, and a
  counter starting again at 0 could revive old entries; a missing token is just a fresh namespace;
- single-flight: one computation per key across workers (SET NX PX lock) and within a process;
- resilience: any cache error opens a circuit for a few seconds (every call bypasses the cache,
  so a dead Valkey costs nothing per request), logged at most once a minute. A cache problem never
  causes an error response.

Entry keys: <CACHE_KEY_PREFIX>c:<CACHE_SCHEMA_VERSION>:<catalog version>:<kind>:<parts...>
"""

import asyncio
import contextvars
import logging
import random
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote

logger = logging.getLogger("app.cache")

# Bump this whenever a CACHED response model changes shape (fields added, removed, renamed or
# re-typed), so entries written by the previous code are never served by the new code.
CACHE_SCHEMA_VERSION = "1"

LOCK_TTL_MS = 3000  # how long one worker may hold the single-flight lock
LOCK_WAIT_S = 1.0  # how long the others wait for its result before computing themselves
LOCK_POLL_S = 0.025
CIRCUIT_OPEN_S = 5.0  # after FAILURES_TO_OPEN consecutive cache errors, bypass the cache this long
FAILURES_TO_OPEN = 3  # one slow call must not switch the cache off for everybody
POOL_SIZE = 32  # connections per process (bounded: no connection storms)
WARM_CONNECTIONS = 8  # opened at startup, so the first burst does not have to connect
WARN_EVERY_S = 60.0
TTL_JITTER = 0.10

# Cache outcome of the current request, for the access log ("hit", "miss", "bypass").
cache_events: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("cache_events", default=None)


def _record(outcome: str) -> None:
    events = cache_events.get()
    if events is not None:
        events.append(outcome)


class CacheBackend(Protocol):
    async def mget(self, keys: Sequence[str]) -> list[bytes | None]: ...
    async def set(self, key: str, value: bytes, ttl_ms: int | None) -> None: ...
    async def set_many(self, values: dict[str, bytes]) -> None: ...
    async def set_nx(self, key: str, value: bytes, ttl_ms: int) -> bool: ...
    async def delete(self, keys: Sequence[str]) -> None: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


class ValkeyBackend:
    """redis-py asyncio client, short timeouts, no retries (the circuit breaker handles errors).

    A BLOCKING pool of POOL_SIZE connections per process: under a burst, requests wait up to the
    timeout for a free connection instead of opening hundreds of new ones (the default pool
    raises "Too many connections" at its limit, and a connection storm through a proxy, Docker's
    port forwarding for example, makes every connect slow)."""

    def __init__(self, url: str, timeout_ms: int, max_connections: int = POOL_SIZE) -> None:
        from redis.asyncio import BlockingConnectionPool, Redis
        from redis.asyncio.retry import Retry
        from redis.backoff import NoBackoff

        timeout = timeout_ms / 1000
        pool = BlockingConnectionPool.from_url(
            url, max_connections=max_connections, timeout=timeout, socket_timeout=timeout,
            socket_connect_timeout=timeout, health_check_interval=0,
        )
        self.client = Redis(connection_pool=pool, retry=Retry(NoBackoff(), 0))

    async def warm(self, connections: int) -> None:
        """Open a few connections now (sequentially), so a burst finds them ready."""
        pool = self.client.connection_pool
        opened = [await pool.get_connection() for _ in range(connections)]
        for connection in opened:
            await pool.release(connection)

    async def mget(self, keys: Sequence[str]) -> list[bytes | None]:
        return list(await self.client.mget(list(keys)))

    async def set(self, key: str, value: bytes, ttl_ms: int | None) -> None:
        await self.client.set(key, value, px=ttl_ms)

    async def set_many(self, values: dict[str, bytes]) -> None:
        await self.client.mset(values)  # type: ignore[arg-type]

    async def set_nx(self, key: str, value: bytes, ttl_ms: int) -> bool:
        return bool(await self.client.set(key, value, px=ttl_ms, nx=True))

    async def delete(self, keys: Sequence[str]) -> None:
        if keys:
            await self.client.delete(*keys)

    async def ping(self) -> bool:
        return bool(await self.client.ping())

    async def close(self) -> None:
        await self.client.aclose()


@dataclass
class MemoryBackend:
    """In-memory fake with the same behaviour (TTL included); used by the tests."""

    now: Callable[[], float] = time.monotonic
    data: dict[str, tuple[bytes, float | None]] = field(default_factory=dict)
    fail: bool = False  # tests: simulate a dead cache server
    calls: int = 0

    def _check(self) -> None:
        self.calls += 1
        if self.fail:
            raise ConnectionError("cache down (simulated)")

    def _get(self, key: str) -> bytes | None:
        item = self.data.get(key)
        if item is None:
            return None
        value, expires = item
        if expires is not None and expires <= self.now():
            del self.data[key]
            return None
        return value

    async def mget(self, keys: Sequence[str]) -> list[bytes | None]:
        self._check()
        return [self._get(k) for k in keys]

    async def set(self, key: str, value: bytes, ttl_ms: int | None) -> None:
        self._check()
        self.data[key] = (value, self.now() + ttl_ms / 1000 if ttl_ms else None)

    async def set_many(self, values: dict[str, bytes]) -> None:
        self._check()
        for key, value in values.items():
            self.data[key] = (value, None)

    async def set_nx(self, key: str, value: bytes, ttl_ms: int) -> bool:
        self._check()
        if self._get(key) is not None:
            return False
        self.data[key] = (value, self.now() + ttl_ms / 1000)
        return True

    async def delete(self, keys: Sequence[str]) -> None:
        self._check()
        for key in keys:
            self.data.pop(key, None)

    async def ping(self) -> bool:
        self._check()
        return True

    async def close(self) -> None:
        return None


def new_token() -> bytes:
    return secrets.token_hex(6).encode()


def key_part(value: str) -> str:
    """Make free text (slugs from the URL, category filters) safe inside a key."""
    return quote(value, safe="-_.~")[:200]


def jittered_ms(ttl_seconds: float) -> int:
    return max(1, int(ttl_seconds * 1000 * random.uniform(1 - TTL_JITTER, 1 + TTL_JITTER)))


class Cache:
    def __init__(self, backend: CacheBackend | None, prefix: str = "lamsa:", schema: str | None = None) -> None:
        self.backend = backend
        self.prefix = prefix
        self.schema = schema or CACHE_SCHEMA_VERSION
        self._open_until = 0.0
        self._failures = 0
        self._last_warning = -WARN_EVERY_S
        self._local_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ resilience

    @property
    def available(self) -> bool:
        return self.backend is not None and time.monotonic() >= self._open_until

    def _failed(self, exc: BaseException) -> None:
        """Count a failure; FAILURES_TO_OPEN in a row open the circuit for CIRCUIT_OPEN_S."""
        self._failures += 1
        if self._failures < FAILURES_TO_OPEN:
            return
        self._failures = 0
        now = time.monotonic()
        self._open_until = now + CIRCUIT_OPEN_S
        if now - self._last_warning >= WARN_EVERY_S:
            self._last_warning = now
            logger.warning("Cache unavailable, bypassing it for %.0f s: %r", CIRCUIT_OPEN_S, exc)

    # -------------------------------------------------------------------- basics

    def key(self, name: str) -> str:
        return f"{self.prefix}{name}"

    async def mget(self, names: Sequence[str]) -> list[bytes | None]:
        if not self.available:
            return [None] * len(names)
        try:
            values = await self.backend.mget([self.key(n) for n in names])  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - any cache problem means "bypass"
            self._failed(exc)
            return [None] * len(names)
        self._failures = 0
        return values

    async def get(self, name: str) -> bytes | None:
        return (await self.mget([name]))[0]

    async def set(self, name: str, value: bytes, ttl_seconds: float | None) -> None:
        if not self.available:
            return
        try:
            await self.backend.set(self.key(name), value, jittered_ms(ttl_seconds) if ttl_seconds else None)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            self._failed(exc)

    async def delete(self, *names: str) -> None:
        if not self.available:
            return
        try:
            await self.backend.delete([self.key(n) for n in names])  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            self._failed(exc)

    # ------------------------------------------------------------------ versions

    async def versions(self, names: Sequence[str]) -> list[str] | None:
        """Current tokens of ``ver:<name>`` (created when missing). None when the cache is
        unavailable: callers then bypass the cache entirely."""
        if not self.available:
            return None
        keys = [f"ver:{n}" for n in names]
        values = await self.mget(keys)
        if not self.available:
            return None
        result: list[str] = []
        for key, value in zip(keys, values, strict=True):
            if value is None:
                token = new_token()
                try:
                    created = await self.backend.set_nx(self.key(key), token, 30 * 24 * 3600 * 1000)  # type: ignore[union-attr]
                    value = token if created else await self.get(key)
                except Exception as exc:  # noqa: BLE001
                    self._failed(exc)
                    return None
                if value is None:
                    return None
            result.append(value.decode())
        return result

    async def bump(self, names: Iterable[str]) -> None:
        """New random tokens: every entry keyed with the old ones is never read again."""
        values = {self.key(f"ver:{n}"): new_token() for n in names}
        if not values or not self.available:
            return
        try:
            await self.backend.set_many(values)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            self._failed(exc)

    def entry(self, catalog_version: str, kind: str, *parts: str) -> str:
        """c:<schema>:<catalog version>:<kind>:<parts>, without the prefix."""
        return ":".join(["c", self.schema, catalog_version, kind, *parts])

    # -------------------------------------------------------------- single-flight

    async def get_or_compute(self, name: str, ttl_seconds: float, compute: Callable[[], Awaitable[bytes]]) -> bytes:
        """The cached bytes, or compute() once (per key, across workers) and store them."""
        cached = await self.get(name)
        if cached is not None:
            _record("hit")
            return cached
        if not self.available:
            _record("bypass")
            return await compute()
        lock = self._local_locks.setdefault(name, asyncio.Lock())
        try:
            async with lock:
                cached = await self.get(name)  # another task of this process may have just filled it
                if cached is not None:
                    _record("hit")
                    return cached
                return await self._compute_once(name, ttl_seconds, compute)
        finally:
            if not lock.locked() and self._local_locks.get(name) is lock:
                self._local_locks.pop(name, None)

    async def _compute_once(self, name: str, ttl_seconds: float, compute: Callable[[], Awaitable[bytes]]) -> bytes:
        lock_key = f"{name}:lock"
        token = new_token()
        owner = False
        if self.available:
            try:
                owner = await self.backend.set_nx(self.key(lock_key), token, LOCK_TTL_MS)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                self._failed(exc)
        if not owner and self.available:
            # Another worker computes it: wait briefly for its result.
            deadline = time.monotonic() + LOCK_WAIT_S
            while time.monotonic() < deadline:
                await asyncio.sleep(LOCK_POLL_S)
                cached = await self.get(name)
                if cached is not None:
                    _record("hit")
                    return cached
                if not self.available:
                    break
        _record("miss")
        try:
            value = await compute()
            await self.set(name, value, ttl_seconds)
            return value
        finally:
            if owner:
                await self.delete(lock_key)

    async def close(self) -> None:
        if self.backend is not None:
            try:
                await self.backend.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------- the instance

_cache: Cache | None = None


def build_cache() -> Cache:
    from app.core.config import settings

    backend = ValkeyBackend(settings.CACHE_URL, settings.CACHE_TIMEOUT_MS) if settings.CACHE_ENABLED else None
    return Cache(backend, settings.CACHE_KEY_PREFIX)


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = build_cache()
    return _cache


def set_cache(cache: Cache | None) -> None:
    """Tests: install a cache over MemoryBackend (None: build from settings again on next use)."""
    global _cache
    _cache = cache


async def start_cache() -> None:
    """At startup: connect (best effort) and bump the catalog version, so a fresh deploy never
    serves entries written by the previous code. Every worker doing it is fine."""
    cache = get_cache()
    if cache.backend is None:
        logger.info("Cache disabled (CACHE_ENABLED=false)")
        return
    try:
        await cache.backend.ping()
        if isinstance(cache.backend, ValkeyBackend):
            await cache.backend.warm(WARM_CONNECTIONS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cache not reachable at startup, requests bypass it until it is: %r", exc)
        return
    await cache.bump(["catalog"])
    logger.info("Cache connected; catalog version bumped")


async def stop_cache() -> None:
    global _cache
    if _cache is not None:
        await _cache.close()
        _cache = None
