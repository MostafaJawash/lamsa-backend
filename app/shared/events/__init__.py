"""A tiny in-process domain event dispatcher (no external broker).

Services collect events in an `EventBuffer` while their transaction runs and call
`buffer.publish()` only AFTER a successful commit, so nothing is published for work that was
rolled back. Handlers run in the request after the commit; a failing handler is logged and never
breaks the request. The notifications phase will register FCM handlers next to the logging one.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("app.events")


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


@dataclass(frozen=True)
class OrderStatusChanged(DomainEvent):
    order_id: int
    user_id: int
    from_status: str | None
    to_status: str
    changed_by_user_id: int | None


@dataclass(frozen=True)
class OrderCancelled(DomainEvent):
    order_id: int
    user_id: int
    by: str  # "customer" | "admin"
    stock_restored: bool


@dataclass(frozen=True)
class StockLow(DomainEvent):
    product_id: int
    variant_id: int | None
    stock: int
    threshold: int


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
    """Events of one transaction; publish() only after the commit succeeded."""

    events: list[DomainEvent] = field(default_factory=list)

    def add(self, event: DomainEvent) -> None:
        self.events.append(event)

    def clear(self) -> None:
        self.events.clear()

    async def publish(self) -> None:
        events, self.events = self.events, []
        for event in events:
            await dispatch(event)


async def log_event(event: DomainEvent) -> None:
    logger.info("%s %s", event.name, _fields(event))


def _fields(event: DomainEvent) -> dict[str, Any]:
    return {k: v for k, v in event.__dict__.items()}


subscribe(DomainEvent, log_event)

__all__ = [
    "DomainEvent",
    "EventBuffer",
    "OrderCancelled",
    "OrderPlaced",
    "OrderStatusChanged",
    "StockLow",
    "dispatch",
    "subscribe",
    "unsubscribe",
]
