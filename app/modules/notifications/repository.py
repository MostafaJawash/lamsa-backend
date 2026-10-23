"""Notification queries. Every function here is ONE statement."""

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ColumnElement,
    and_,
    case,
    delete,
    exists,
    false,
    func,
    insert,
    literal,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.enums import NotificationStatus, NotificationTarget, NotificationType
from app.modules.notifications.models import Notification, NotificationRead, UserAlertLog
from app.modules.users.enums import UserRole
from app.modules.users.models import User, UserDevice

# ------------------------------------------------------------------------------- inbox


def inbox_condition(user_id: Any, notify_marketing: Any) -> ColumnElement[bool]:
    """Rows a user sees: sent, and theirs or a broadcast. Marketing switched off is hidden.

    user_id / notify_marketing are Python values or columns (User.id, User.notify_marketing)."""
    if isinstance(notify_marketing, bool):
        notify_marketing = true() if notify_marketing else false()
    return and_(
        Notification.status == NotificationStatus.sent,
        or_(
            Notification.user_id == user_id,
            and_(
                Notification.target == NotificationTarget.all,
                or_(Notification.type != NotificationType.marketing, notify_marketing),
            ),
        ),
    )


def _read_by(user_id: Any) -> ColumnElement[bool]:
    return and_(NotificationRead.notification_id == Notification.id, NotificationRead.user_id == user_id)


async def inbox_page(session: AsyncSession, user: User, offset: int, limit: int) -> list[Any]:
    """(Notification, read_at) rows, newest first."""
    rows = await session.execute(
        select(Notification, NotificationRead.read_at)
        .outerjoin(NotificationRead, _read_by(user.id))
        .where(inbox_condition(user.id, user.notify_marketing))
        .order_by(Notification.sent_at.desc(), Notification.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(rows.all())


async def inbox_totals(session: AsyncSession, user: User) -> tuple[int, int]:
    """(total, unread) in one statement."""
    row = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(case((NotificationRead.user_id.is_(None), 1), else_=0)), 0),
            )
            .select_from(Notification)
            .outerjoin(NotificationRead, _read_by(user.id))
            .where(inbox_condition(user.id, user.notify_marketing))
        )
    ).one()
    return int(row[0]), int(row[1])


def _unread_count_expr(user_id: Any, notify_marketing: Any) -> Any:
    return (
        select(func.count())
        .select_from(Notification)
        .where(inbox_condition(user_id, notify_marketing), ~exists().where(_read_by(user_id)))
        .scalar_subquery()
    )


async def unread_count(session: AsyncSession, user: User) -> int:
    return int(await session.scalar(select(_unread_count_expr(user.id, user.notify_marketing))) or 0)


async def unread_counts(session: AsyncSession, user_ids: Iterable[int]) -> dict[int, int]:
    """Badge numbers for many users at once (correlated subquery, one statement)."""
    ids = sorted(set(user_ids))
    if not ids:
        return {}
    rows = await session.execute(
        select(User.id, _unread_count_expr(User.id, User.notify_marketing)).where(User.id.in_(ids))
    )
    return {uid: int(count) for uid, count in rows.all()}


async def mark_read(session: AsyncSession, user: User, notification_id: int, now: datetime) -> int:
    """INSERT IGNORE ... SELECT: 1 when newly read, 0 when already read OR not visible."""
    source = select(Notification.id, literal(user.id), literal(now)).where(
        Notification.id == notification_id, inbox_condition(user.id, user.notify_marketing)
    )
    result = await session.execute(
        insert(NotificationRead)
        .prefix_with("IGNORE")
        .from_select(["notification_id", "user_id", "read_at"], source)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def is_in_inbox(session: AsyncSession, user: User, notification_id: int) -> bool:
    found = await session.scalar(
        select(Notification.id).where(Notification.id == notification_id, inbox_condition(user.id, user.notify_marketing))
    )
    return found is not None


async def mark_all_read(session: AsyncSession, user: User, now: datetime) -> int:
    source = select(Notification.id, literal(user.id), literal(now)).where(
        inbox_condition(user.id, user.notify_marketing), ~exists().where(_read_by(user.id))
    )
    result = await session.execute(
        insert(NotificationRead)
        .prefix_with("IGNORE")
        .from_select(["notification_id", "user_id", "read_at"], source)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


# ------------------------------------------------------------------------------ outbox


async def insert_rows(session: AsyncSession, rows: Sequence[dict[str, Any]]) -> None:
    """Many notifications in ONE INSERT (no ids needed)."""
    if rows:
        await session.execute(insert(Notification).values(list(rows)))


async def insert_alert_log(session: AsyncSession, rows: Sequence[dict[str, Any]]) -> None:
    if rows:
        await session.execute(insert(UserAlertLog).values(list(rows)))


async def active_admin_ids(session: AsyncSession) -> list[int]:
    rows = await session.scalars(
        select(User.id).where(User.role == UserRole.admin, User.is_active.is_(True)).order_by(User.id)
    )
    return list(rows)


def due_condition(now: datetime) -> ColumnElement[bool]:
    return or_(
        Notification.status == NotificationStatus.queued,
        and_(Notification.status == NotificationStatus.scheduled, Notification.scheduled_at <= now),
    )


async def claim_due(session: AsyncSession, now: datetime, limit: int) -> list[Notification]:
    """Lock due rows; concurrent dispatchers skip each other's rows."""
    rows = await session.scalars(
        select(Notification)
        .where(due_condition(now))
        .order_by(Notification.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(rows)


async def set_sending(session: AsyncSession, ids: Sequence[int], now: datetime) -> None:
    """sent_at holds the claim time while sending (used to recover stuck rows)."""
    await session.execute(
        update(Notification)
        .where(Notification.id.in_(ids))
        .values(status=NotificationStatus.sending, sent_at=now)
        .execution_options(synchronize_session=False)
    )


async def finish(session: AsyncSession, notification_id: int, now: datetime, recipients: int, success: int, failure: int) -> None:
    await session.execute(
        update(Notification)
        .where(Notification.id == notification_id)
        .values(status=NotificationStatus.sent, sent_at=now, recipients_count=recipients,
                success_count=success, failure_count=failure)
        .execution_options(synchronize_session=False)
    )


async def requeue_stuck(session: AsyncSession, older_than: datetime) -> int:
    result = await session.execute(
        update(Notification)
        .where(Notification.status == NotificationStatus.sending, Notification.sent_at < older_than)
        .values(status=NotificationStatus.queued)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def recipient_condition(type_: NotificationType) -> ColumnElement[bool]:
    """Who still gets a push of this type at send time (active, preference on, admin alerts
    only for admins)."""
    conditions: list[ColumnElement[bool]] = [User.is_active.is_(True)]
    if type_ == NotificationType.marketing:
        conditions.append(User.notify_marketing.is_(True))
    elif type_ in (NotificationType.favorite_back_in_stock, NotificationType.favorite_price_drop):
        conditions.append(User.notify_favorites.is_(True))
    elif type_ == NotificationType.cart_reminder:
        conditions.append(User.notify_cart_reminders.is_(True))
    elif type_ == NotificationType.admin_alert:
        conditions.append(User.role == UserRole.admin)
    return and_(*conditions)


async def user_devices(session: AsyncSession, user_id: int, type_: NotificationType) -> list[Any]:
    rows = await session.execute(
        select(UserDevice.fcm_token, UserDevice.user_id)
        .join(User, User.id == UserDevice.user_id)
        .where(UserDevice.user_id == user_id, recipient_condition(type_))
        .order_by(UserDevice.id)
    )
    return list(rows.all())


async def broadcast_devices(session: AsyncSession, type_: NotificationType, after_id: int, limit: int) -> list[Any]:
    """Keyset page of every recipient device: (id, fcm_token, user_id)."""
    rows = await session.execute(
        select(UserDevice.id, UserDevice.fcm_token, UserDevice.user_id)
        .join(User, User.id == UserDevice.user_id)
        .where(UserDevice.id > after_id, recipient_condition(type_))
        .order_by(UserDevice.id)
        .limit(limit)
    )
    return list(rows.all())


async def delete_devices(session: AsyncSession, tokens: Iterable[str]) -> None:
    tokens = list(tokens)
    if tokens:
        await session.execute(delete(UserDevice).where(UserDevice.fcm_token.in_(tokens)))


async def devices_of(session: AsyncSession, user_id: int) -> list[str]:
    return list(await session.scalars(select(UserDevice.fcm_token).where(UserDevice.user_id == user_id).order_by(UserDevice.id)))


# ------------------------------------------------------------------------------- admin


async def admin_page(
    session: AsyncSession, offset: int, limit: int, conditions: Sequence[ColumnElement[bool]]
) -> tuple[int, list[Any]]:
    """(total, [(Notification, creator name, recipient name)]): 2 statements."""
    total = int(await session.scalar(select(func.count()).select_from(Notification).where(*conditions)) or 0)
    creator = User.__table__.alias("creator")
    recipient = User.__table__.alias("recipient")
    rows = await session.execute(
        select(Notification, creator.c.name, recipient.c.name)
        .outerjoin(creator, creator.c.id == Notification.created_by_user_id)
        .outerjoin(recipient, recipient.c.id == Notification.user_id)
        .where(*conditions)
        .order_by(Notification.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return total, list(rows.all())


async def get_with_names(session: AsyncSession, notification_id: int) -> Any:
    creator = User.__table__.alias("creator")
    recipient = User.__table__.alias("recipient")
    return (
        await session.execute(
            select(Notification, creator.c.name, recipient.c.name)
            .outerjoin(creator, creator.c.id == Notification.created_by_user_id)
            .outerjoin(recipient, recipient.c.id == Notification.user_id)
            .where(Notification.id == notification_id)
        )
    ).first()


async def get_for_update(session: AsyncSession, notification_id: int) -> Notification | None:
    return await session.scalar(select(Notification).where(Notification.id == notification_id).with_for_update())


# ----------------------------------------------------------------------------- cleanup


async def delete_old_notifications(session: AsyncSession, before: datetime, limit: int) -> int:
    ids = select(Notification.id).where(
        or_(
            and_(Notification.status == NotificationStatus.sent, Notification.sent_at < before),
            and_(Notification.status == NotificationStatus.cancelled, Notification.created_at < before),
        )
    ).limit(limit)
    # MySQL cannot DELETE with a LIMIT subquery on the same table: wrap it in a derived table.
    result = await session.execute(
        delete(Notification).where(Notification.id.in_(select(ids.subquery().c.id)))
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def delete_old_alert_log(session: AsyncSession, before: datetime, limit: int) -> int:
    ids = select(UserAlertLog.id).where(UserAlertLog.created_at < before).limit(limit)
    result = await session.execute(delete(UserAlertLog).where(UserAlertLog.id.in_(select(ids.subquery().c.id))))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
