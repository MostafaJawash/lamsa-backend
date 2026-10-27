"""Cached public endpoints: application cache + HTTP caching headers.

- The ANONYMOUS response body (JSON bytes) is cached under a versioned key. is_favorite is never
  stored: for a logged-in user the cached body is parsed and is_favorite is overlaid with ONE query.
- Anonymous requests get `Cache-Control: public, max-age=<ttl>, stale-while-revalidate=60`;
  requests with an Authorization header get `private, no-cache`. Both get `Vary: Authorization`
  and a weak ETag of the body; `If-None-Match` with the same ETag is answered with 304.
- Every other response gets `Cache-Control: no-store` from the middleware (app.core.middleware).
"""

import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import Request, Response
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache_events, get_cache, key_part
from app.modules.favorites.repository import favorite_ids_among
from app.modules.settings.service import catalog_version
from app.shared.catalog_cache import product_version_name

STALE_WHILE_REVALIDATE = 60

# Time to live per kind (seconds).
TTL = {
    "categories": 300,
    "category": 300,
    "brands": 300,
    "brand": 300,
    "banners": 60,
    "products": 45,
    "facets": 45,
    "product": 120,  # long on purpose: freshness comes from the catalog and per-product version bumps
    "related": 120,
    "products_ids": 45,
}

# Query parameters that change a product list / facets response (anything else is ignored by the
# API, so it must not split the cache either).
LIST_KEYS = frozenset({"category", "brand", "q", "min_price", "max_price", "in_stock", "on_sale", "is_new"})
PAGING_KEYS = frozenset({"page", "page_size", "sort"})
BOOLEAN_KEYS = frozenset({"in_stock", "on_sale", "is_new"})
DECIMAL_KEYS = frozenset({"min_price", "max_price"})
TRUE_VALUES = frozenset({"1", "true", "yes", "on", "t", "y"})

Overlay = Callable[[bytes], Awaitable[bytes]]


def is_authenticated(request: Request) -> bool:
    return "authorization" in request.headers


def etag_of(body: bytes) -> str:
    return 'W/"' + hashlib.blake2b(body, digest_size=12).hexdigest() + '"'


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    wanted = etag.removeprefix("W/")
    return any(tag.strip() == "*" or tag.strip().removeprefix("W/") == wanted for tag in header.split(","))


def cached_json_response(request: Request, body: bytes, ttl: int) -> Response:
    headers = {
        "Cache-Control": "private, no-cache" if is_authenticated(request)
        else f"public, max-age={ttl}, stale-while-revalidate={STALE_WHILE_REVALIDATE}",
        "Vary": "Authorization",
        "ETag": etag_of(body),
    }
    if _etag_matches(request.headers.get("if-none-match"), headers["ETag"]):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


def _canonical(key: str, value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if key in BOOLEAN_KEYS:
        return "true" if value.lower() in TRUE_VALUES else "false"
    if key in DECIMAL_KEYS:
        try:
            return format(Decimal(value).normalize(), "f")
        except InvalidOperation:
            return value
    if key in ("page", "page_size"):
        return str(int(value)) if value.isdigit() else value
    return value


def normalized_query(pairs: Iterable[tuple[str, str]], *, paging: bool, defaults: dict[str, str] | None = None) -> str:
    """Same parameters in any order (or repeated) give the same string; unknown parameters and
    empty values are dropped; booleans and decimals are written one way; defaults filled in."""
    seen: set[tuple[str, str]] = set()
    allowed = LIST_KEYS | PAGING_KEYS if paging else LIST_KEYS
    for key, value in pairs:
        if key in allowed or key.startswith(("attr.", "opt.")):
            canonical = _canonical(key, value)
            if canonical is not None and not (key in BOOLEAN_KEYS and canonical == "false"):
                seen.add((key, canonical))
    present = {k for k, _ in seen}
    for key, value in (defaults or {}).items():
        if paging and key not in present:
            seen.add((key, value))
    return "&".join(f"{k}={v}" for k, v in sorted(seen))


def query_hash(normalized: str) -> str:
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


async def serve_cached(
    request: Request,
    kind: str,
    parts: Sequence[str],
    adapter: TypeAdapter[Any],
    compute: Callable[[], Awaitable[Any]],
    *,
    product_slug: str | None = None,
    overlay: Overlay | None = None,
) -> Response:
    """Cached anonymous body (computed once per key), overlay for logged-in users, HTTP headers."""
    ttl = TTL[kind]
    cache = get_cache()

    async def produce() -> bytes:
        return adapter.dump_json(await compute())

    names = ["catalog"] + ([product_version_name(product_slug)] if product_slug is not None else [])
    versions = await cache.versions(names)
    token = catalog_version.set(versions[0] if versions is not None else None)
    try:
        if versions is None:
            events = cache_events.get()
            if events is not None:
                events.append("bypass")
            body = await produce()
        else:
            key_parts = [key_part(p) for p in parts]
            if product_slug is not None:
                name = cache.entry(versions[0], "p", versions[1], kind, *key_parts)
            else:
                name = cache.entry(versions[0], kind, *key_parts)
            body = await cache.get_or_compute(name, ttl, produce)
    finally:
        catalog_version.reset(token)
    if overlay is not None:
        body = await overlay(body)
    return cached_json_response(request, body, ttl)


# ------------------------------------------------------------------------ is_favorite


def _dump(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()


def favorites_overlay(session: AsyncSession, user_id: int | None, shape: str) -> Overlay | None:
    """shape: "page" ({items: [cards]}), "list" ([cards]) or "one" (a detail). None for anonymous."""
    if user_id is None:
        return None

    async def overlay(body: bytes) -> bytes:
        data = json.loads(body)
        cards: list[dict[str, Any]] = data["items"] if shape == "page" else data if shape == "list" else [data]
        favorites = await favorite_ids_among(session, user_id, [c["id"] for c in cards])
        for card in cards:
            card["is_favorite"] = card["id"] in favorites
        return _dump(data)

    return overlay
