"""A tiny in-process domain event dispatcher (no external broker).

THE one commit-then-publish path: `commit_and_publish(session, buffer)`.

- Services collect events in an `EventBuffer` while their transaction runs.
- Code deep inside a transaction (e.g. recalculate_product_summary) stashes events on the
  session with `stash(session, event)`; a rollback of that session clears the stash.
- `commit_and_publish` commits first and only then dispatches the buffer's and the stashed
  events, so nothing is ever published for work that was rolled back.

Handlers run in the same process after the commit; a failing handler is logged and never breaks
the request. Push notifications are created by handlers in app.modules.notifications.handlers.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

logger = logging.getLogger("app.events")

STASH_KEY = "lamsa.pending_events"


@dataclass(frozen=True)
class DomainEvent:
    @property
    def name(self) -> str:
        return type(self).__name__


@dataclass(frozen=True)
class OrderPlaced(DomainEvent):
    order_id: int
    user_id: int
    total: str
    items_count: int = 0
    delivery_method_name: str = ""
    is_pickup: bool = False


@dataclass(frozen=True)
class OrderStatusChanged(DomainEvent):
    order_id: int
    user_id: int
    from_status: str | None
    to_status: str
    changed_by_user_id: int | None
    is_pickup: bool = False


@dataclass(frozen=True)
class OrderCancelled(DomainEvent):
    order_id: int
    user_id: int
    by: str  # "customer" | "admin"
    stock_restored: bool
    reason: str | None = None


@dataclass(frozen=True)
class StockLow(DomainEvent):
    product_id: int
    variant_id: int | None
    stock: int
    threshold: int


@dataclass(frozen=True)
class StockOut(DomainEvent):
    """Stock reached 0 (through an order or an admin stock change)."""

    product_id: int
    variant_id: int | None


@dataclass(frozen=True)
class ProductBackInStock(DomainEvent):
    """in_stock went false -> true while the product is visible."""

    product_id: int


@dataclass(frozen=True)
class ProductPriceDropped(DomainEvent):
    """A visible, in-stock product started a sale or its price_from went down (the handler
    applies the price_drop_min_percent threshold)."""

    product_id: int
    old_price_from: Decimal
    new_price_from: Decimal
    sale_started: bool


@dataclass(frozen=True)
class CatalogChanged(DomainEvent):
    """An admin changed something customers see (categories, definitions, brands, products,
    variants, bundle items, images, banners). Bumps the global catalog cache version."""


@dataclass(frozen=True)
class ProductsChanged(DomainEvent):
    """These products' summary or stock may have changed (orders, cancellations, stock adjusts,
    edits). Bumps their product-detail cache versions."""

    slugs: tuple[str, ...]


Handler = Callable[[DomainEvent], Awaitable[None]]
_handlers: list[tuple[type[DomainEvent], Handler]] = []


def subscribe(event_type: type[DomainEvent], handler: Handler) -> None:
    _handlers.append((event_type, handler))


def unsubscribe(handler: Handler) -> None:
    _handlers[:] = [(t, h) for t, h in _handlers if h is not handler]


async def dispatch(event: DomainEvent) -> None:
    for event_type, handler in list(_handlers):
        if isinstance(event, event_type):
            try:
                await handler(event)
            except Exception:
                logger.exception("Event handler %s failed for %s", getattr(handler, "__name__", handler), event)


@dataclass
class EventBuffer:
    """Events of one transaction. Published only by commit_and_publish()."""

    events: list[DomainEvent] = field(default_factory=list)

    def add(self, event: DomainEvent) -> None:
        self.events.append(event)

    def clear(self) -> None:
        self.events.clear()


def stash(session: AsyncSession | Session, event: DomainEvent) -> None:
    """Remember an event on the session's transaction (cleared on rollback)."""
    session.info.setdefault(STASH_KEY, []).append(event)


def _take_stash(session: AsyncSession | Session) -> list[DomainEvent]:
    events: list[DomainEvent] = session.info.pop(STASH_KEY, [])
    return events


@sa_event.listens_for(Session, "after_soft_rollback")
def _clear_stash_on_rollback(session: Session, previous_transaction: Any) -> None:
    session.info.pop(STASH_KEY, None)


async def commit_and_publish(session: AsyncSession, buffer: EventBuffer | None = None) -> None:
    """Commit, then publish the buffer's and the session's stashed events (in that order)."""
    try:
        await session.commit()
    except BaseException:
        _take_stash(session)
        if buffer is not None:
            buffer.clear()
        raise
    events = [*(buffer.events if buffer is not None else []), *_take_stash(session)]
    if buffer is not None:
        buffer.clear()
    for event in events:
        await dispatch(event)


async def log_event(event: DomainEvent) -> None:
    logger.info("%s %s", event.name, dict(event.__dict__))


subscribe(DomainEvent, log_event)

__all__ = [
    "CatalogChanged",
    "DomainEvent",
    "EventBuffer",
    "OrderCancelled",
    "OrderPlaced",
    "OrderStatusChanged",
    "ProductBackInStock",
    "ProductPriceDropped",
    "ProductsChanged",
    "StockLow",
    "StockOut",
    "commit_and_publish",
    "dispatch",
    "stash",
    "subscribe",
    "unsubscribe",
]
