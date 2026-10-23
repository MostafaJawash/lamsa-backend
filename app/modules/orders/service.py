"""Orders: checkout, customer history and cancellation, admin workflow, stats.

Each public method is one transaction and commits itself; domain events are published only
after the commit. See app.modules.orders.stock for the locking order.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock
from app.modules.cart import repository as cart_repo
from app.modules.cart.schemas import CartMergeItem, CartMergeOut
from app.modules.cart.service import CartService, CheckoutSnapshot, delivery_quote, line_image, line_prices
from app.modules.orders import repository as repo
from app.modules.orders import stock, workflow
from app.modules.orders.enums import OrderStatus, PaymentMethod, PaymentStatus
from app.modules.orders.models import Order, OrderItem, OrderStatusHistory
from app.modules.orders.schemas import (
    AdminOrderDetailOut,
    AdminOrderSummaryOut,
    DeliveryOut,
    HistoryOut,
    OrderCreate,
    OrderCustomer,
    OrderDetailOut,
    OrderItemOut,
    OrderSort,
    OrderStats,
    OrderSummaryOut,
    TimelineStepOut,
    TopProduct,
)
from app.modules.products.enums import ProductType
from app.modules.products.summary import recalculate_product_summary
from app.modules.products.validation import variant_label
from app.modules.users.models import User
from app.shared.catalog import field_error
from app.shared.dependencies.pagination import Page, PageParams
from app.shared.events import EventBuffer, OrderCancelled, OrderPlaced, OrderStatusChanged
from app.shared.exceptions import Conflict, ErrorCode, NotFound, ValidationFailed
from app.shared.money import format_money
from app.shared.schemas import image_urls

logger = logging.getLogger(__name__)

IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ZERO = Decimal("0.00")


def validate_idempotency_key(key: str | None) -> str | None:
    if key is None:
        return None
    if not IDEMPOTENCY_KEY.match(key):
        raise field_error("Idempotency-Key", "1..64 characters: letters, digits, - and _")
    return key


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


# ============================================================================ presenting


def is_pickup(order: Order) -> bool:
    return order.shipping_address is None


def items_out(items: Sequence[OrderItem], slugs: dict[int, str]) -> list[OrderItemOut]:
    return [
        OrderItemOut(
            product_id=i.product_id, product_slug=slugs.get(i.product_id) if i.product_id else None,
            name=i.product_name, variant_label=i.variant_label, options=i.options, image=image_urls(i.image_key),
            unit_price=i.unit_price, quantity=i.quantity, line_total=i.line_total, bundle_contents=i.bundle_contents,
        )
        for i in items
    ]


def detail_out(
    order: Order, items: Sequence[OrderItem], history: Sequence[workflow.HistoryEntry], slugs: dict[int, str],
    *, can_cancel: bool,
) -> dict[str, Any]:
    pickup = is_pickup(order)
    return {
        "id": order.id, "created_at": order.created_at, "status": order.status,
        "status_label": workflow.label(order.status, pickup), "payment_method": order.payment_method,
        "payment_status": order.payment_status,
        "delivery": DeliveryOut(code=order.delivery_method_code, name=order.delivery_method_name),
        "shipping_address": order.shipping_address, "customer_notes": order.customer_notes,
        "gift_message": order.gift_message, "items": items_out(items, slugs), "subtotal": order.subtotal,
        "discount": order.discount, "shipping_fee": order.shipping_fee, "total": order.total,
        "timeline": [
            TimelineStepOut(status=s.status, label=s.label, reached_at=s.reached_at, is_current=s.is_current,
                            cancel_reason=s.cancel_reason)
            for s in workflow.timeline(order.status, pickup, history, order.cancel_reason)
        ],
        "cancel_reason": order.cancel_reason, "can_cancel": can_cancel,
    }


def summary_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.id, "created_at": row.created_at, "status": row.status,
        "status_label": workflow.label(row.status, row.shipping_address is None), "items_count": int(row.items_count),
        "total": row.total, "image": image_urls(row.image_key), "delivery_method_name": row.delivery_method_name,
    }


# ============================================================================ cancelling


async def cancel_order(
    session: AsyncSession, order: Order, *, actor_id: int | None, by: str, reason: str | None, restore_stock: bool,
    events: EventBuffer,
) -> None:
    """THE cancellation (customer and admin). The order row must already be locked (FOR UPDATE);
    cancelled is terminal, so stock can never be restored twice. Does not commit."""
    if restore_stock:
        needs, skipped = stock.needs_from_order_items(await repo.items_of(session, order.id))
        for note in skipped:
            logger.warning("Order %s: %s", order.id, note)
        touched = await stock.restore(session, needs, order.id)
        await recalculate_product_summary(session, touched)
    previous = order.status
    order.status = OrderStatus.cancelled
    order.cancel_reason = reason
    session.add(OrderStatusHistory(
        order_id=order.id, from_status=previous, to_status=OrderStatus.cancelled, changed_by_user_id=actor_id,
        note=reason, created_at=clock.utcnow(),
    ))
    events.add(OrderCancelled(order_id=order.id, user_id=order.user_id, by=by, stock_restored=restore_stock))


# ============================================================================== customer


@dataclass(frozen=True)
class PlacedOrder:
    detail: OrderDetailOut
    created: bool


class OrderService:
    def __init__(self, session: AsyncSession, user: User) -> None:
        self.session = session
        self.user = user
        self.user_id = user.id  # plain int: a retry's rollback expires ORM objects
        self.events = EventBuffer()

    # ------------------------------------------------------------------- placing

    async def place(self, data: OrderCreate, key: str | None) -> PlacedOrder:
        cart = CartService(self.session, self.user)

        async def operation() -> PlacedOrder:
            self.events.clear()
            result = await self._place(cart, data, key)
            await self.events.publish()
            return result

        # Retried once on a deadlock or a duplicate Idempotency-Key race (the retry then finds
        # the order committed by the parallel request and returns it).
        return await cart.retrying(operation)

    async def _place(self, cart: CartService, data: OrderCreate, key: str | None) -> PlacedOrder:
        # 1. Per-user lock (+ the cart line references), then idempotency with a locking read.
        limits, refs = await cart_repo.lock_user_with_lines(self.session, self.user_id)
        if key is not None:
            existing = await repo.order_by_key(self.session, self.user_id, key)
            if existing is not None:
                await self.session.commit()
                return PlacedOrder(await self.get(existing.id), created=False)
        if not refs:
            raise ValidationFailed(ErrorCode.CART_EMPTY)

        # 2. Lock every product and variant involved, in ascending id order (see stock.py).
        product_ids = {r.product_id for r in refs}
        variant_ids = {r.variant_id for r in refs if r.variant_id is not None}
        bundle_ids = {r.product_id for r in refs if r.product_type == ProductType.bundle}
        await cart.loader.ensure_components(bundle_ids)
        for bundle_id in bundle_ids:
            for component in cart.loader.components.get(bundle_id, []):
                product_ids.add(component.product_id)
                if component.variant_id is not None:
                    variant_ids.add(component.variant_id)
        containing = await stock.bundles_containing(self.session, product_ids)
        locked = await stock.lock_rows(self.session, product_ids | containing, variant_ids)

        # 3. The cart exactly as GET /cart computes it (current values: locking read).
        snapshot = await cart.checkout_snapshot(limits)
        if not snapshot.rows:
            raise ValidationFailed(ErrorCode.CART_EMPTY)
        if not snapshot.cart.can_checkout:
            raise Conflict(ErrorCode.CART_HAS_ISSUES, details={"cart": snapshot.cart.model_dump(mode="json")})

        # 4. Delivery method and address.
        method = snapshot.method(data.delivery_method_code)  # the active methods the cart quoted
        if method is None:
            raise ValidationFailed(ErrorCode.DELIVERY_METHOD_NOT_FOUND, details={"code": data.delivery_method_code})
        address = None
        if method.requires_address:
            if data.address_id is None:
                raise ValidationFailed(ErrorCode.ADDRESS_REQUIRED)
            address = await repo.user_address(self.session, self.user_id, data.address_id)
            if address is None:
                raise NotFound(ErrorCode.ADDRESS_NOT_FOUND)
        elif data.address_id is not None:
            raise field_error("address_id", "pickup orders take no address")

        # 5-6. Totals with the cart's own functions, compared with what the customer saw.
        subtotal = snapshot.cart.subtotal
        shipping_fee = delivery_quote(method, subtotal).fee
        discount = ZERO
        total = subtotal - discount + shipping_fee
        if total != data.expected_total:
            raise Conflict(ErrorCode.PRICE_CHANGED, details={
                "expected_total": format_money(data.expected_total), "total": format_money(total),
                "cart": snapshot.cart.model_dump(mode="json"),
            })

        # 7. Take the stock (one conditional UPDATE per table).
        needs = self._needs(snapshot)
        low_stock = await stock.deduct(self.session, needs, locked, limits.low_stock_threshold)

        # 8-10. The order, its items and the first history row (snapshots).
        now = clock.utcnow()
        user = self.user
        order = Order(
            user_id=self.user_id, customer_name=user.name, customer_phone=user.phone,
            shipping_address={
                "label": address.label, "city": address.city, "street": address.street,
                "building": address.building, "notes": address.notes,
            } if address is not None else None,
            delivery_method_id=method.id, delivery_method_code=method.code, delivery_method_name=method.name,
            payment_method=PaymentMethod.cod, payment_status=PaymentStatus.pending, status=OrderStatus.pending,
            subtotal=subtotal, discount=discount, shipping_fee=shipping_fee, total=total,
            customer_notes=_blank_to_none(data.customer_notes), gift_message=_blank_to_none(data.gift_message),
            idempotency_key=key, created_at=now, updated_at=now,
        )
        self.session.add(order)
        await self.session.flush()
        items = self._items(order.id, snapshot)
        await self.session.execute(insert(OrderItem), [self._item_row(i) for i in items])
        self.session.add(OrderStatusHistory(
            order_id=order.id, from_status=None, to_status=OrderStatus.pending, changed_by_user_id=self.user_id,
            created_at=now,
        ))

        # 11-12. Empty the cart; recalculate summaries (and the bundles containing them).
        await cart_repo.delete_lines(self.session, self.user_id)
        await recalculate_product_summary(self.session, needs.product_ids | bundle_ids)
        await self.session.commit()

        # 13. Events (published by place() after this returns, i.e. after the commit).
        self.events.add(OrderPlaced(order_id=order.id, user_id=self.user_id, total=format_money(total)))
        for event in low_stock:
            self.events.add(event)
        history = [workflow.HistoryEntry(OrderStatus.pending, now)]
        slugs = {r.product_id: r.slug for r in snapshot.rows}
        detail = OrderDetailOut(**detail_out(order, items, history, slugs, can_cancel=True))
        return PlacedOrder(detail, created=True)

    @staticmethod
    def _needs(snapshot: CheckoutSnapshot) -> stock.StockNeeds:
        needs = stock.StockNeeds()
        for row in snapshot.rows:
            if row.type == ProductType.variable and row.variant_id is not None:
                needs.add_variant(row.product_id, row.variant_id, row.quantity)
            elif row.type == ProductType.bundle:
                for c in snapshot.loader.components.get(row.product_id, []):
                    if c.variant_id is not None:
                        needs.add_variant(c.product_id, c.variant_id, c.quantity * row.quantity)
                    else:
                        needs.add_product(c.product_id, c.quantity * row.quantity)
            else:
                needs.add_product(row.product_id, row.quantity)
        return needs

    @staticmethod
    def _items(order_id: int, snapshot: CheckoutSnapshot) -> list[OrderItem]:
        items: list[OrderItem] = []
        for row in snapshot.rows:
            prices = line_prices(row)
            options = dict(row.variant_options) if row.variant_options is not None else None
            items.append(OrderItem(
                order_id=order_id, product_id=row.product_id, variant_id=row.variant_id, product_name=row.name,
                variant_label=variant_label(options) if options else None, options=options,
                sku=row.variant_sku or row.product_sku,
                image_key=line_image(row, snapshot.images.get(row.product_id, []), snapshot.loader),
                unit_price=prices.current, quantity=row.quantity, line_total=prices.current * row.quantity,
                bundle_contents=[
                    {"product_id": c.product_id, "variant_id": c.variant_id, "name": c.product_name,
                     "variant_label": variant_label(c.variant_options) if c.variant_options else None,
                     "quantity": c.quantity}
                    for c in snapshot.loader.components.get(row.product_id, [])
                ] if row.type == ProductType.bundle else None,
            ))
        return items

    @staticmethod
    def _item_row(item: OrderItem) -> dict[str, Any]:
        return {
            "order_id": item.order_id, "product_id": item.product_id, "variant_id": item.variant_id,
            "product_name": item.product_name, "variant_label": item.variant_label, "options": item.options,
            "sku": item.sku, "image_key": item.image_key, "unit_price": item.unit_price, "quantity": item.quantity,
            "line_total": item.line_total, "bundle_contents": item.bundle_contents,
        }

    # --------------------------------------------------------------------- reads

    async def list(self, params: PageParams, status: OrderStatus | None) -> Page[OrderSummaryOut]:
        rows, conditions = await repo.list_orders(
            self.session, user_id=self.user_id, status=status, offset=params.offset, limit=params.page_size
        )
        total = int(rows[0].total_rows) if rows else (await repo.count_orders(self.session, conditions) if params.page > 1 else 0)
        return Page[OrderSummaryOut](
            items=[OrderSummaryOut(**summary_out(r)) for r in rows], page=params.page, page_size=params.page_size, total=total
        )

    async def get(self, order_id: int) -> OrderDetailOut:
        order = await repo.get_order(self.session, order_id, user_id=self.user_id)
        if order is None:
            raise NotFound(ErrorCode.ORDER_NOT_FOUND)
        items = await repo.items_of(self.session, order.id)
        history = await repo.history_of(self.session, order.id)
        slugs = await repo.visible_slugs(self.session, [i.product_id for i in items if i.product_id])
        entries = [workflow.HistoryEntry(h.to_status, h.created_at) for h in history]
        return OrderDetailOut(**detail_out(order, items, entries, slugs,
                                           can_cancel=workflow.can_customer_cancel(order.status)))

    # ---------------------------------------------------------------- cancelling

    async def cancel(self, order_id: int, reason: str | None) -> OrderDetailOut:
        order = await repo.get_order(self.session, order_id, user_id=self.user_id, for_update=True)
        if order is None:
            raise NotFound(ErrorCode.ORDER_NOT_FOUND)
        if not workflow.can_customer_cancel(order.status):
            raise Conflict(ErrorCode.ORDER_NOT_CANCELLABLE, details={"status": order.status.value})
        await cancel_order(self.session, order, actor_id=self.user_id, by="customer",
                           reason=_blank_to_none(reason), restore_stock=True, events=self.events)
        await self.session.commit()
        await self.events.publish()
        return await self.get(order_id)

    async def reorder(self, order_id: int) -> CartMergeOut:
        """Put the order's items back in the cart with the cart's merge rules."""
        order = await repo.get_order(self.session, order_id, user_id=self.user_id)
        if order is None:
            raise NotFound(ErrorCode.ORDER_NOT_FOUND)
        items = await repo.items_of(self.session, order.id)
        # A deleted product (product_id NULL) gets the placeholder id 0, which merge skips as
        # PRODUCT_NOT_FOUND; the report then shows product_id null for it.
        merge_items = [CartMergeItem(product_id=i.product_id or 0, variant_id=i.variant_id, quantity=i.quantity) for i in items]
        result = await CartService(self.session, self.user).merge(merge_items)
        for entry in result.merge_report:
            if entry.product_id == 0:
                entry.product_id = None
        return result


# ================================================================================= admin


class AdminOrderService:
    def __init__(self, session: AsyncSession, admin: User) -> None:
        self.session = session
        self.admin_id = admin.id
        self.events = EventBuffer()

    async def list(
        self, params: PageParams, *, status: OrderStatus | None, delivery_method: str | None, q: str | None,
        date_from: date | None, date_to: date | None, sort: OrderSort,
    ) -> Page[AdminOrderSummaryOut]:
        rows, conditions = await repo.list_orders(
            self.session, user_id=None, status=status, delivery_method=delivery_method, q=q, date_from=date_from,
            date_to=date_to, sort=sort, offset=params.offset, limit=params.page_size,
        )
        total = int(rows[0].total_rows) if rows else (await repo.count_orders(self.session, conditions) if params.page > 1 else 0)
        return Page[AdminOrderSummaryOut](
            items=[
                AdminOrderSummaryOut(**summary_out(r), user_id=r.user_id, customer_name=r.customer_name,
                                     customer_phone=r.customer_phone)
                for r in rows
            ],
            page=params.page, page_size=params.page_size, total=total,
        )

    async def get(self, order_id: int) -> AdminOrderDetailOut:
        order = await repo.get_order(self.session, order_id)
        if order is None:
            raise NotFound(ErrorCode.ORDER_NOT_FOUND)
        items = await repo.items_of(self.session, order.id)
        history = await repo.history_of(self.session, order.id)
        slugs = await repo.visible_slugs(self.session, [i.product_id for i in items if i.product_id])
        customer = await repo.customer_with_orders_count(self.session, order.user_id)
        entries = [workflow.HistoryEntry(h.to_status, h.created_at) for h in history]
        base = detail_out(order, items, entries, slugs, can_cancel=workflow.can_admin_cancel(order.status))
        user, orders_count = customer if customer else (None, 0)
        return AdminOrderDetailOut(
            **base,
            customer=OrderCustomer(
                user_id=order.user_id, name=user.name if user else order.customer_name,
                phone=user.phone if user else order.customer_phone, orders_count=orders_count,
            ),
            history=[
                HistoryOut(from_status=h.from_status, to_status=h.to_status, changed_by_user_id=h.changed_by_user_id,
                           changed_by_name=h.changed_by_name, note=h.note, created_at=h.created_at)
                for h in history
            ],
            allowed_next_statuses=workflow.allowed_next(order.status, is_pickup(order)),
        )

    async def change_status(self, order_id: int, to_status: OrderStatus, note: str | None) -> AdminOrderDetailOut:
        order = await repo.get_order(self.session, order_id, for_update=True)
        if order is None:
            raise NotFound(ErrorCode.ORDER_NOT_FOUND)
        allowed = workflow.allowed_next(order.status, is_pickup(order))
        if to_status not in allowed:
            raise ValidationFailed(ErrorCode.INVALID_STATUS_TRANSITION, details={
                "from": order.status.value, "to": to_status.value, "allowed": [s.value for s in allowed],
            })
        previous = order.status
        order.status = to_status
        if to_status == OrderStatus.delivered:
            order.payment_status = PaymentStatus.paid
        self.session.add(OrderStatusHistory(
            order_id=order.id, from_status=previous, to_status=to_status, changed_by_user_id=self.admin_id,
            note=_blank_to_none(note), created_at=clock.utcnow(),
        ))
        self.events.add(OrderStatusChanged(order_id=order.id, user_id=order.user_id, from_status=previous.value,
                                           to_status=to_status.value, changed_by_user_id=self.admin_id))
        await self.session.commit()
        await self.events.publish()
        return await self.get(order_id)

    async def cancel(self, order_id: int, reason: str, restore_stock: bool) -> AdminOrderDetailOut:
        order = await repo.get_order(self.session, order_id, for_update=True)
        if order is None:
            raise NotFound(ErrorCode.ORDER_NOT_FOUND)
        if not workflow.can_admin_cancel(order.status):
            raise Conflict(ErrorCode.ORDER_NOT_CANCELLABLE, details={"status": order.status.value})
        if not restore_stock and order.status != OrderStatus.shipped:
            raise field_error("restore_stock", "stock can only be kept off (lost goods) for shipped orders")
        await cancel_order(self.session, order, actor_id=self.admin_id, by="admin", reason=reason,
                           restore_stock=restore_stock, events=self.events)
        await self.session.commit()
        await self.events.publish()
        return await self.get(order_id)

    async def stats(self, date_from: date | None, date_to: date | None) -> OrderStats:
        conditions = repo.utc_range(date_from, date_to)
        totals = await repo.status_totals(self.session, conditions)
        top = await repo.top_products(self.session, conditions)
        orders_count = sum(count for count, _ in totals.values())
        delivered_count, delivered_revenue = totals.get(OrderStatus.delivered, (0, ZERO))
        cancelled = totals.get(OrderStatus.cancelled, (0, ZERO))[0]
        return OrderStats(
            date_from=date_from, date_to=date_to, orders_count=orders_count,
            counts={s.value: totals.get(s, (0, ZERO))[0] for s in OrderStatus},
            delivered_revenue=delivered_revenue,
            average_order_value=(delivered_revenue / delivered_count).quantize(Decimal("0.01")) if delivered_count else ZERO,
            cancelled_rate=round(cancelled / orders_count, 4) if orders_count else 0.0,
            top_products=[TopProduct(product_id=p, name=n, quantity=q) for p, n, q in top],
        )
