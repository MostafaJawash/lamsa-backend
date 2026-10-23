"""Domain event -> notification rows. Runs after the publishing transaction committed, in its own
session (runtime.notification_session); never pushes itself, it only kicks the dispatcher.

Statements per run (measured in tests/notifications/test_notification_budgets.py, commit excluded;
independent of the number of admins or fans): OrderPlaced 2, OrderStatusChanged 1,
OrderCancelled 1 (by admin) / 2 (by customer), StockLow / StockOut 3, ProductBackInStock 4,
ProductPriceDropped 4 (1 when below the threshold).
"""

import logging
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock
from app.modules.favorites.models import Favorite
from app.modules.notifications import repository as repo
from app.modules.notifications import templates
from app.modules.notifications.enums import NotificationType
from app.modules.notifications.links import Link
from app.modules.notifications.models import UserAlertLog
from app.modules.notifications.outbox import Draft, add_drafts
from app.modules.notifications.runtime import kick_dispatch, notification_session
from app.modules.orders.enums import OrderStatus
from app.modules.products.models import Product, ProductVariant
from app.modules.products.validation import variant_label
from app.modules.products.visibility import visible_product_condition
from app.modules.settings.service import NotificationSettings, load_notification_settings
from app.modules.users.models import User
from app.shared.enums import LinkType
from app.shared.events import (
    DomainEvent,
    OrderCancelled,
    OrderPlaced,
    OrderStatusChanged,
    ProductBackInStock,
    ProductPriceDropped,
    StockLow,
    StockOut,
    subscribe,
    unsubscribe,
)
from app.shared.money import format_money

logger = logging.getLogger(__name__)


def _order_link(order_id: int) -> Link:
    return Link(LinkType.order, str(order_id))


def _customer_order_draft(order_id: int, user_id: int, text: templates.Text) -> Draft:
    return Draft(NotificationType.order_update, text, user_id, link=_order_link(order_id),
                 order_id=order_id, order_owner_id=user_id)


def _admin_drafts(admin_ids: list[int], text: templates.Text, link: Link, order_id: int | None = None) -> list[Draft]:
    return [Draft(NotificationType.admin_alert, text, admin_id, link=link, order_id=order_id) for admin_id in admin_ids]


async def _save(session: AsyncSession, drafts: list[Draft], admin_ids: list[int] | None = None,
                settings: NotificationSettings | None = None) -> None:
    if not drafts:
        return
    await add_drafts(session, drafts, admin_ids=admin_ids or (), settings=settings)
    await session.commit()
    kick_dispatch()


# -------------------------------------------------------------------------------- orders

STATUS_TEXTS = {
    OrderStatus.preparing: templates.order_preparing,
    OrderStatus.shipped: templates.order_shipped,
    OrderStatus.ready_for_pickup: templates.order_ready_for_pickup,
}


async def on_order_placed(event: DomainEvent) -> None:
    assert isinstance(event, OrderPlaced)
    async with notification_session() as session:
        admin_ids = await repo.active_admin_ids(session)  # admins active NOW get it, nobody else
        drafts = [_customer_order_draft(event.order_id, event.user_id, templates.order_placed(event.order_id))]
        text = templates.admin_new_order(event.order_id, event.items_count, event.total, event.delivery_method_name)
        drafts += _admin_drafts(admin_ids, text, Link(LinkType.admin_order, str(event.order_id)), event.order_id)
        await _save(session, drafts, admin_ids)


async def on_order_status_changed(event: DomainEvent) -> None:
    assert isinstance(event, OrderStatusChanged)
    status = OrderStatus(event.to_status)
    if status == OrderStatus.delivered:
        text = templates.order_delivered(event.order_id, event.is_pickup)
    elif status in STATUS_TEXTS:
        text = STATUS_TEXTS[status](event.order_id)
    else:
        return
    async with notification_session() as session:
        await _save(session, [_customer_order_draft(event.order_id, event.user_id, text)])


async def on_order_cancelled(event: DomainEvent) -> None:
    assert isinstance(event, OrderCancelled)
    async with notification_session() as session:
        if event.by == "admin":
            text = templates.order_cancelled_by_store(event.order_id, event.reason)
            await _save(session, [_customer_order_draft(event.order_id, event.user_id, text)])
            return
        # The customer cancelled: nothing for them, an alert for the admins.
        admin_ids = await repo.active_admin_ids(session)
        text = templates.admin_customer_cancelled(event.order_id, event.reason)
        await _save(session, _admin_drafts(admin_ids, text, Link(LinkType.admin_order, str(event.order_id)),
                                           event.order_id), admin_ids)


# --------------------------------------------------------------------------------- stock


async def _product_label(session: AsyncSession, product_id: int, variant_id: int | None) -> tuple[str, str | None] | None:
    """(product name, variant label or None): ONE statement."""
    query = select(Product.name, ProductVariant.options).where(Product.id == product_id)
    query = query.outerjoin(ProductVariant, (ProductVariant.product_id == Product.id) & (ProductVariant.id == variant_id))
    row = (await session.execute(query)).first()
    if row is None:
        return None
    label = variant_label(row.options) if row.options else None
    return row.name, label or None


async def _stock_alert(product_id: int, variant_id: int | None,
                       make_text: Callable[[str, str | None], templates.Text]) -> None:
    async with notification_session() as session:
        label = await _product_label(session, product_id, variant_id)
        if label is None:
            return
        admin_ids = await repo.active_admin_ids(session)
        text = make_text(*label)
        await _save(session, _admin_drafts(admin_ids, text, Link(LinkType.admin_product, str(product_id))), admin_ids)


async def on_stock_low(event: DomainEvent) -> None:
    assert isinstance(event, StockLow)
    stock = event.stock
    await _stock_alert(event.product_id, event.variant_id, lambda name, label: templates.admin_stock_low(name, label, stock))


async def on_stock_out(event: DomainEvent) -> None:
    assert isinstance(event, StockOut)
    await _stock_alert(event.product_id, event.variant_id, templates.admin_stock_out)


# ---------------------------------------------------------------------------- favorites


async def _favorite_alert(product_id: int, kind: NotificationType, settings: NotificationSettings,
                          session: AsyncSession) -> None:
    """Every fan of a visible, in-stock product, preference on, outside the cooldown: one select,
    one insert of notifications, one insert of the alert log."""
    now = clock.now_utc()
    since = now - timedelta(days=settings.favorite_alert_cooldown_days)
    rows = (
        await session.execute(
            select(Favorite.user_id, Product.name, Product.slug, Product.price_from)
            .join(Product, Product.id == Favorite.product_id)
            .join(User, User.id == Favorite.user_id)
            .where(
                Favorite.product_id == product_id,
                User.is_active.is_(True),
                User.notify_favorites.is_(True),
                Product.in_stock.is_(True),
                visible_product_condition(),
                ~exists().where(
                    UserAlertLog.user_id == Favorite.user_id,
                    UserAlertLog.kind == kind,
                    UserAlertLog.ref_id == product_id,
                    UserAlertLog.created_at >= since,
                ),
            )
            .order_by(Favorite.user_id)
        )
    ).all()
    if not rows:
        return
    name, slug, price = rows[0].name, rows[0].slug, rows[0].price_from
    if kind == NotificationType.favorite_back_in_stock:
        text = templates.favorite_back_in_stock(name)
    else:
        text = templates.favorite_price_drop(name, format_money(price))
    link = Link(LinkType.product, slug)
    drafts = [Draft(kind, text, r.user_id, link=link) for r in rows]
    await add_drafts(session, drafts, settings=settings)
    await repo.insert_alert_log(session, [{"user_id": r.user_id, "kind": kind, "ref_id": product_id, "created_at": now}
                                          for r in rows])
    await session.commit()
    kick_dispatch()


def price_drop_percent(old: Decimal, new: Decimal) -> Decimal:
    if old <= 0:
        return Decimal(0)
    return (old - new) / old * 100


async def on_back_in_stock(event: DomainEvent) -> None:
    assert isinstance(event, ProductBackInStock)
    async with notification_session() as session:
        settings = await load_notification_settings(session)
        await _favorite_alert(event.product_id, NotificationType.favorite_back_in_stock, settings, session)


async def on_price_dropped(event: DomainEvent) -> None:
    assert isinstance(event, ProductPriceDropped)
    async with notification_session() as session:
        settings = await load_notification_settings(session)
        if not event.sale_started and price_drop_percent(event.old_price_from, event.new_price_from) < settings.price_drop_min_percent:
            return
        await _favorite_alert(event.product_id, NotificationType.favorite_price_drop, settings, session)


HANDLERS = (
    (OrderPlaced, on_order_placed),
    (OrderStatusChanged, on_order_status_changed),
    (OrderCancelled, on_order_cancelled),
    (StockLow, on_stock_low),
    (StockOut, on_stock_out),
    (ProductBackInStock, on_back_in_stock),
    (ProductPriceDropped, on_price_dropped),
)


def register() -> None:
    """Subscribe every handler (idempotent)."""
    for event_type, handler in HANDLERS:
        unsubscribe(handler)
        subscribe(event_type, handler)


def unregister() -> None:
    for _, handler in HANDLERS:
        unsubscribe(handler)
