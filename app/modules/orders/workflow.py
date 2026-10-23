"""Order status workflow: THE transition table, labels and timeline. Used everywhere.

Delivery (the order has a shipping address):  pending -> preparing -> shipped -> delivered
Pickup (no shipping address):                 pending -> preparing -> ready_for_pickup -> delivered
Cancellation: customers from pending only; admins from pending, preparing, shipped or
ready_for_pickup. delivered and cancelled are terminal.

Whether an order is a pickup is decided by its own snapshot (shipping_address is NULL), so later
changes to a delivery method never change the workflow of existing orders.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.modules.orders.enums import OrderStatus

DELIVERY_FLOW: tuple[OrderStatus, ...] = (
    OrderStatus.pending, OrderStatus.preparing, OrderStatus.shipped, OrderStatus.delivered,
)
PICKUP_FLOW: tuple[OrderStatus, ...] = (
    OrderStatus.pending, OrderStatus.preparing, OrderStatus.ready_for_pickup, OrderStatus.delivered,
)
CUSTOMER_CANCELLABLE = frozenset({OrderStatus.pending})
ADMIN_CANCELLABLE = frozenset(
    {OrderStatus.pending, OrderStatus.preparing, OrderStatus.shipped, OrderStatus.ready_for_pickup}
)
TERMINAL = frozenset({OrderStatus.delivered, OrderStatus.cancelled})

LABELS: dict[OrderStatus, str] = {
    OrderStatus.pending: "استلمنا طلبك",
    OrderStatus.preparing: "قيد التجهيز",
    OrderStatus.shipped: "انشحن",
    OrderStatus.ready_for_pickup: "جاهز للاستلام",
    OrderStatus.delivered: "وصل",
    OrderStatus.cancelled: "ملغي",
}
PICKUP_LABELS: dict[OrderStatus, str] = {OrderStatus.delivered: "تم الاستلام"}


def flow(is_pickup: bool) -> tuple[OrderStatus, ...]:
    return PICKUP_FLOW if is_pickup else DELIVERY_FLOW


def label(status: OrderStatus, is_pickup: bool) -> str:
    return PICKUP_LABELS.get(status, LABELS[status]) if is_pickup else LABELS[status]


def allowed_next(status: OrderStatus, is_pickup: bool) -> list[OrderStatus]:
    """Forward moves an admin may make: exactly one step. Cancelling is a separate action."""
    steps = flow(is_pickup)
    if status not in steps or status == steps[-1]:
        return []
    return [steps[steps.index(status) + 1]]


def can_customer_cancel(status: OrderStatus) -> bool:
    return status in CUSTOMER_CANCELLABLE


def can_admin_cancel(status: OrderStatus) -> bool:
    return status in ADMIN_CANCELLABLE


@dataclass(frozen=True)
class HistoryEntry:
    to_status: OrderStatus
    created_at: datetime


@dataclass(frozen=True)
class TimelineStep:
    status: OrderStatus
    label: str
    reached_at: datetime | None
    is_current: bool
    cancel_reason: str | None = None


def timeline(
    status: OrderStatus, is_pickup: bool, history: Sequence[HistoryEntry], cancel_reason: str | None
) -> list[TimelineStep]:
    """Every step of the order's workflow in order. A cancelled order shows the steps it
    reached, then the cancelled step."""
    reached: dict[OrderStatus, datetime] = {}
    for entry in sorted(history, key=lambda h: h.created_at):
        reached.setdefault(entry.to_status, entry.created_at)
    if status == OrderStatus.cancelled:
        steps = [
            TimelineStep(s, label(s, is_pickup), reached[s], False) for s in flow(is_pickup) if s in reached
        ]
        steps.append(
            TimelineStep(OrderStatus.cancelled, label(OrderStatus.cancelled, is_pickup),
                         reached.get(OrderStatus.cancelled), True, cancel_reason)
        )
        return steps
    return [TimelineStep(s, label(s, is_pickup), reached.get(s), s == status) for s in flow(is_pickup)]
