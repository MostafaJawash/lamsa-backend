"""Order queries. Each function is ONE SQL statement."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, case, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.modules.delivery.models import DeliveryMethod
from app.modules.orders.enums import OrderStatus
from app.modules.orders.models import Order, OrderItem, OrderStatusHistory, OrderTotal
from app.modules.orders.schemas import OrderSort, OrderStatusGroup
from app.modules.products.models import Product
from app.modules.products.visibility import visible_product_condition
from app.modules.users.models import User, UserAddress


async def order_by_key(session: AsyncSession, user_id: int, key: str) -> Order | None:
    """Locking read (FOR SHARE): sees an order committed by a parallel request with the same key."""
    return await session.scalar(
        select(Order).where(Order.user_id == user_id, Order.idempotency_key == key).with_for_update(read=True)
    )


async def get_order(
    session: AsyncSession, order_id: int, *, user_id: int | None = None,
    for_update: bool = False, with_totals: bool = False,
) -> Order | None:
    stmt = select(Order).where(Order.id == order_id)
    if user_id is not None:
        stmt = stmt.where(Order.user_id == user_id)
    if for_update:
        stmt = stmt.with_for_update()
    if with_totals:
        stmt = stmt.options(joinedload(Order.totals))
    result = await session.execute(stmt)
    return result.unique().scalar_one_or_none()


async def items_of(session: AsyncSession, order_id: int) -> list[OrderItem]:
    return list((await session.scalars(select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.id))).all())


async def totals_of(session: AsyncSession, order_id: int) -> list[OrderTotal]:
    result = await session.scalars(
        select(OrderTotal)
        .where(OrderTotal.order_id == order_id)
        .order_by(case((OrderTotal.currency == "SYP", 0), (OrderTotal.currency == "USD", 1), else_=2))
    )
    return list(result.all())


async def totals_for_orders(session: AsyncSession, order_ids: Sequence[int]) -> dict[int, list[OrderTotal]]:
    if not order_ids:
        return {}
    rows = await session.scalars(
        select(OrderTotal)
        .where(OrderTotal.order_id.in_(order_ids))
        .order_by(OrderTotal.order_id, case((OrderTotal.currency == "SYP", 0), (OrderTotal.currency == "USD", 1), else_=2))
    )
    result: dict[int, list[OrderTotal]] = {}
    for total in rows:
        result.setdefault(total.order_id, []).append(total)
    return result


@dataclass(frozen=True)
class HistoryRow:
    from_status: OrderStatus | None
    to_status: OrderStatus
    changed_by_user_id: int | None
    changed_by_name: str | None
    note: str | None
    created_at: datetime


async def history_of(session: AsyncSession, order_id: int) -> list[HistoryRow]:
    rows = await session.execute(
        select(
            OrderStatusHistory.from_status, OrderStatusHistory.to_status, OrderStatusHistory.changed_by_user_id,
            User.name, OrderStatusHistory.note, OrderStatusHistory.created_at,
        )
        .outerjoin(User, User.id == OrderStatusHistory.changed_by_user_id)
        .where(OrderStatusHistory.order_id == order_id)
        .order_by(OrderStatusHistory.created_at, OrderStatusHistory.id)
    )
    return [HistoryRow(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]


async def visible_slugs(session: AsyncSession, product_ids: Sequence[int]) -> dict[int, str]:
    if not product_ids:
        return {}
    rows = await session.execute(
        select(Product.id, Product.slug).where(Product.id.in_(product_ids), visible_product_condition())
    )
    return {r.id: r.slug for r in rows}


async def active_delivery_method(session: AsyncSession, code: str) -> DeliveryMethod | None:
    return await session.scalar(
        select(DeliveryMethod).where(DeliveryMethod.code == code, DeliveryMethod.is_active.is_(True))
    )


async def user_address(session: AsyncSession, user_id: int, address_id: int) -> UserAddress | None:
    return await session.scalar(
        select(UserAddress).where(UserAddress.id == address_id, UserAddress.user_id == user_id)
    )


async def customer_with_orders_count(session: AsyncSession, user_id: int) -> tuple[User, int] | None:
    count = select(func.count(Order.id)).where(Order.user_id == User.id).correlate(User).scalar_subquery()
    row = (await session.execute(select(User, count).where(User.id == user_id))).first()
    return (row[0], int(row[1])) if row else None


# --------------------------------------------------------------------------------- lists


def _summary_select(*extra: Any) -> Select[Any]:
    items_count = (
        select(func.coalesce(func.sum(OrderItem.quantity), 0))
        .where(OrderItem.order_id == Order.id).correlate(Order).scalar_subquery()
    )
    first_image = (
        select(OrderItem.image_key)
        .where(OrderItem.order_id == Order.id).order_by(OrderItem.id).limit(1).correlate(Order).scalar_subquery()
    )
    return select(
        Order.id, Order.created_at, Order.status, Order.delivery_method_name, Order.shipping_address,
        Order.user_id, Order.customer_name, Order.customer_phone,
        items_count.label("items_count"), first_image.label("image_key"), func.count().over().label("total_rows"),
        *extra,
    )


PHONE_SEPARATORS = re.compile(r"[\s+\-().]")
MAX_ORDER_ID = 2**63 - 1


@dataclass(frozen=True)
class OrderQuery:
    """Admin search text, normalized once.

    Numeric input (digits, ignoring spaces, +, -, brackets and dots) matches the order number
    exactly OR the customer phone containing those digits. Leading zeros are dropped for the
    phone comparison, so local "0933 123 472" and "00963 933..." match "+963933...".
    """

    text: str
    is_numeric: bool
    order_id: int | None
    phone_digits: str


def parse_order_query(q: str) -> OrderQuery:
    text = q.strip()
    digits = PHONE_SEPARATORS.sub("", text)
    if not digits.isdigit():
        return OrderQuery(text=text, is_numeric=False, order_id=None, phone_digits="")
    number = int(digits)
    return OrderQuery(
        text=text,
        is_numeric=True,
        order_id=number if 0 < number <= MAX_ORDER_ID else None,
        phone_digits=digits.lstrip("0"),
    )


def utc_range(date_from: date | None, date_to: date | None) -> list[Any]:
    """created_at is UTC; date_to is inclusive (the whole day)."""
    conditions: list[Any] = []
    if date_from is not None:
        conditions.append(Order.created_at >= datetime.combine(date_from, time.min))
    if date_to is not None:
        conditions.append(Order.created_at < datetime.combine(date_to + timedelta(days=1), time.min))
    return conditions


async def list_orders(
    session: AsyncSession,
    *,
    user_id: int | None,
    status: OrderStatus | None,
    status_group: OrderStatusGroup | None = None,
    delivery_method: str | None = None,
    q: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    sort: OrderSort = OrderSort.newest,
    offset: int,
    limit: int,
) -> tuple[list[Any], list[Any]]:
    """One page plus its total (COUNT(*) OVER()). Returns (rows, conditions) so the caller can
    count separately only when the page is past the end."""
    conditions: list[Any] = [*utc_range(date_from, date_to)]
    if user_id is not None:
        conditions.append(Order.user_id == user_id)
    if status is not None:
        conditions.append(Order.status == status)
    if status_group is not None:
        grouped = {
            OrderStatusGroup.active: (
                OrderStatus.pending, OrderStatus.preparing, OrderStatus.shipped, OrderStatus.ready_for_pickup
            ),
            OrderStatusGroup.completed: (OrderStatus.delivered,),
            OrderStatusGroup.cancelled: (OrderStatus.cancelled,),
        }[status_group]
        conditions.append(Order.status.in_(grouped))
    if delivery_method:
        conditions.append(Order.delivery_method_code == delivery_method)
    exact_first: list[Any] = []
    if q:
        search = parse_order_query(q)
        if search.is_numeric:
            matches = []
            if search.order_id is not None:
                matches.append(Order.id == search.order_id)
                # Exact order-number match first, then the chosen sort.
                exact_first = [case((Order.id == search.order_id, 0), else_=1)]
            if search.phone_digits:
                # customer_phone is E.164 ("+963..."): compare digits only.
                matches.append(func.replace(Order.customer_phone, "+", "").contains(search.phone_digits, autoescape=True))
            conditions.append(or_(*matches) if matches else false())
        else:
            conditions.append(
                or_(Order.customer_name.contains(search.text, autoescape=True),
                    Order.customer_phone.contains(search.text, autoescape=True))
            )
    syp_total = (
        select(OrderTotal.total)
        .where(OrderTotal.order_id == Order.id, OrderTotal.currency == "SYP")
        .correlate(Order).scalar_subquery()
    )
    usd_total = (
        select(OrderTotal.total)
        .where(OrderTotal.order_id == Order.id, OrderTotal.currency == "USD")
        .correlate(Order).scalar_subquery()
    )
    order_by = {
        OrderSort.newest: [Order.created_at.desc(), Order.id.desc()],
        OrderSort.oldest: [Order.created_at.asc(), Order.id.asc()],
        OrderSort.total_desc: [func.coalesce(syp_total, 0).desc(), func.coalesce(usd_total, 0).desc(), Order.id.desc()],
    }[sort]
    rows = (
        await session.execute(
            _summary_select().where(*conditions).order_by(*exact_first, *order_by).offset(offset).limit(limit)
        )
    ).all()
    return list(rows), conditions


async def count_orders(session: AsyncSession, conditions: Sequence[Any]) -> int:
    return int(await session.scalar(select(func.count(Order.id)).where(*conditions)) or 0)


# --------------------------------------------------------------------------------- stats


async def status_totals(
    session: AsyncSession, conditions: Sequence[Any]
) -> tuple[dict[OrderStatus, int], dict[tuple[OrderStatus, str], tuple[int, Decimal]]]:
    count_rows = await session.execute(
        select(Order.status, func.count(Order.id)).where(*conditions).group_by(Order.status)
    )
    rows = await session.execute(
        select(Order.status, OrderTotal.currency, func.count(Order.id), func.coalesce(func.sum(OrderTotal.total), 0))
        .join(OrderTotal, OrderTotal.order_id == Order.id)
        .where(*conditions).group_by(Order.status, OrderTotal.currency)
    )
    return (
        {r[0]: int(r[1]) for r in count_rows},
        {(r[0], r[1]): (int(r[2]), Decimal(r[3])) for r in rows},
    )


async def top_products(session: AsyncSession, conditions: Sequence[Any], limit: int = 5) -> list[tuple[int | None, str, int]]:
    """Best sellers by quantity among delivered orders, from the order item snapshots."""
    quantity = func.sum(OrderItem.quantity)
    rows = await session.execute(
        select(OrderItem.product_id, func.max(OrderItem.product_name), quantity)
        .join(Order, Order.id == OrderItem.order_id)
        .where(and_(*conditions, Order.status == OrderStatus.delivered))
        .group_by(OrderItem.product_id)
        .order_by(quantity.desc(), OrderItem.product_id)
        .limit(limit)
    )
    return [(r[0], r[1], int(r[2])) for r in rows]
