"""Periodic notification jobs (run by app.workers)."""

import logging
from datetime import timedelta

from sqlalchemy import exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock
from app.modules.cart.models import CartItem
from app.modules.notifications import repository as repo
from app.modules.notifications import templates
from app.modules.notifications.enums import NotificationType
from app.modules.notifications.models import UserAlertLog
from app.modules.notifications.outbox import Draft, add_drafts
from app.modules.notifications.runtime import kick_dispatch, notification_session
from app.modules.orders.models import Order
from app.modules.products.models import Product
from app.modules.products.visibility import visible_product_condition
from app.modules.settings.service import load_notification_settings
from app.modules.users.models import User

logger = logging.getLogger(__name__)

CART_REMINDER_LOCK = "lamsa.cart_reminders"
# Carts left alone for longer than after_hours + this window are not reminded any more.
CART_REMINDER_WINDOW = timedelta(hours=72)
CART_REMINDER_BATCH = 500
CLEANUP_CHUNK = 1000


async def _get_lock(session: AsyncSession, name: str) -> bool:
    return bool(await session.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": name}))


async def _release_lock(session: AsyncSession, name: str) -> None:
    await session.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": name})


async def cart_reminders() -> int:
    """One reminder per cart change: the cart changed between after_hours (+ window) ago, no
    order since, no reminder since, preference on, and at least one line still available.
    Returns how many reminders were created. Only one worker runs it at a time (GET_LOCK)."""
    async with notification_session() as session:
        if not await _get_lock(session, CART_REMINDER_LOCK):
            logger.info("cart_reminders: another worker holds the lock")
            return 0
        try:
            return await _cart_reminders(session)
        finally:
            await _release_lock(session, CART_REMINDER_LOCK)


async def _cart_reminders(session: AsyncSession) -> int:
    settings = await load_notification_settings(session)
    now = clock.now_utc()
    latest = now - timedelta(hours=settings.cart_reminder_after_hours)
    user_ids = list(
        await session.scalars(
            select(User.id)
            .where(
                User.is_active.is_(True),
                User.notify_cart_reminders.is_(True),
                User.cart_changed_at <= latest,
                User.cart_changed_at > latest - CART_REMINDER_WINDOW,
                exists().where(CartItem.user_id == User.id),
                ~exists().where(Order.user_id == User.id, Order.created_at >= User.cart_changed_at),
                ~exists().where(
                    UserAlertLog.user_id == User.id,
                    UserAlertLog.kind == NotificationType.cart_reminder,
                    UserAlertLog.created_at >= User.cart_changed_at,
                ),
            )
            .order_by(User.id)
            .limit(CART_REMINDER_BATCH)
        )
    )
    if not user_ids:
        return 0
    # Batched availability: every line of every candidate, in cart order, in ONE statement.
    lines = (
        await session.execute(
            select(CartItem.user_id, Product.name, (Product.in_stock.is_(True) & visible_product_condition()).label("ok"))
            .join(Product, Product.id == CartItem.product_id)
            .where(CartItem.user_id.in_(user_ids))
            .order_by(CartItem.user_id, CartItem.id)
        )
    ).all()
    per_user: dict[int, list[tuple[str, bool]]] = {}
    for line in lines:
        per_user.setdefault(line.user_id, []).append((line.name, bool(line.ok)))
    drafts: list[Draft] = []
    for user_id, items in per_user.items():
        first = next((name for name, ok in items if ok), None)
        if first is None:
            continue  # nothing in the cart can be bought right now
        drafts.append(Draft(NotificationType.cart_reminder, templates.cart_reminder(first, len(items) - 1), user_id))
    if not drafts:
        return 0
    await add_drafts(session, drafts, settings=settings)
    await repo.insert_alert_log(
        session, [{"user_id": d.user_id, "kind": NotificationType.cart_reminder, "ref_id": None, "created_at": now}
                  for d in drafts]
    )
    await session.commit()
    kick_dispatch()
    return len(drafts)


async def cleanup() -> tuple[int, int]:
    """Delete sent/cancelled notifications older than notifications_retention_days, and alert-log
    rows older than both the retention and the cooldown (chunks of 1000)."""
    async with notification_session() as session:
        settings = await load_notification_settings(session)
        now = clock.now_utc()
        before = now - timedelta(days=settings.notifications_retention_days)
        log_before = now - timedelta(days=max(settings.notifications_retention_days, settings.favorite_alert_cooldown_days))
        notifications = alerts = 0
        while (count := await repo.delete_old_notifications(session, before, CLEANUP_CHUNK)) > 0:
            notifications += count
            await session.commit()
            if count < CLEANUP_CHUNK:
                break
        while (count := await repo.delete_old_alert_log(session, log_before, CLEANUP_CHUNK)) > 0:
            alerts += count
            await session.commit()
            if count < CLEANUP_CHUNK:
                break
        await session.commit()
    if notifications or alerts:
        logger.info("cleanup: deleted %d notifications and %d alert-log rows", notifications, alerts)
    return notifications, alerts


__all__ = ["cart_reminders", "cleanup"]
