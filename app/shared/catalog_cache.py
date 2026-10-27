"""Catalog cache invalidation: versions bumped after successful commits only.

- Admin catalog routers depend on `catalog_write`: every POST/PUT/PATCH/DELETE stashes
  CatalogChanged on the request's session; commit_and_publish publishes it after the commit, and
  `on_catalog_changed` bumps the global catalog version (every catalog/banner entry key contains it).
- recalculate_product_summary stashes ProductsChanged(slugs): `on_products_changed` bumps those
  products' detail versions, so a product page is fresh right after an order sold the last unit.
  When a visible product's in_stock flips (sold out, or back in stock) it also stashes
  CatalogChanged: browsing hides out-of-stock products (catalog_hide_out_of_stock), so lists,
  facets, counts and related products are recomputed. Other stock changes (10 -> 9) leave lists
  up to their TTL stale (checkout re-validates stock anyway).
- The admin settings endpoint (app.modules.settings.router) is a catalog write too.
"""

import logging

from fastapi import Request

from app.core.cache import get_cache, key_part
from app.core.local_cache import local_cache
from app.shared.dependencies.auth import DbSession
from app.shared.events import CatalogChanged, DomainEvent, ProductsChanged, stash, subscribe, unsubscribe

logger = logging.getLogger(__name__)

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def product_version_name(slug: str) -> str:
    return f"product:{key_part(slug)}"


async def catalog_write(request: Request, session: DbSession) -> None:
    """Router dependency for admin catalog routers (categories, brands, products, banners)."""
    if request.method in WRITE_METHODS:
        stash(session, CatalogChanged())


async def bump_catalog() -> None:
    """Invalidate every catalog/banner entry and this process's settings copy (scripts, tests,
    direct SQL changes)."""
    local_cache.clear()
    await get_cache().bump(["catalog"])


async def on_catalog_changed(event: DomainEvent) -> None:
    await bump_catalog()


async def on_products_changed(event: DomainEvent) -> None:
    assert isinstance(event, ProductsChanged)
    if event.slugs:
        await get_cache().bump([product_version_name(s) for s in event.slugs])


def register() -> None:
    for event_type, handler in ((CatalogChanged, on_catalog_changed), (ProductsChanged, on_products_changed)):
        unsubscribe(handler)
        subscribe(event_type, handler)
