"""Inbox, preferences and admin campaigns. Services own the transaction."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock
from app.modules.notifications import repository as repo
from app.modules.notifications.dispatcher import send_test
from app.modules.notifications.enums import NotificationStatus, NotificationTarget, NotificationType
from app.modules.notifications.links import (
    Link,
    is_visible,
    validate_catalog_link,
    visible_link_targets,
)
from app.modules.notifications.models import Notification
from app.modules.notifications.outbox import NO_LINK, Draft, plan
from app.modules.notifications.quiet_hours import effective_schedule
from app.modules.notifications.runtime import kick_dispatch
from app.modules.notifications.schemas import (
    AdminNotificationCreate,
    AdminNotificationOut,
    AdminNotificationUpdate,
    InboxPage,
    LinkIn,
    LinkOut,
    NotificationOut,
    PreferencesOut,
    PreferencesUpdate,
    PushTestIn,
    PushTestOut,
    PushTestResultOut,
    UnreadCountOut,
)
from app.modules.notifications.templates import Text
from app.modules.settings.service import load_notification_settings
from app.modules.users.enums import UserRole
from app.modules.users.models import User
from app.shared.dependencies.pagination import Page, PageParams
from app.shared.enums import ADMIN_LINK_TYPES, LinkType
from app.shared.exceptions import Conflict, ErrorCode, NotFound, ValidationFailed

SCHEDULE_MIN_AHEAD = timedelta(minutes=1)
SCHEDULE_MAX_AHEAD = timedelta(days=90)
CAMPAIGN_TYPES = (NotificationType.marketing, NotificationType.message)

# ------------------------------------------------------------------------------- inbox


def _inbox_link(n: Notification, user: User, visible: set[tuple[LinkType, str]]) -> LinkOut:
    link = Link(n.link_type, n.link_value)
    if link.type in ADMIN_LINK_TYPES and user.role != UserRole.admin:
        return LinkOut(type=LinkType.none)  # never show admin links to a customer
    if not is_visible(link, visible):
        return LinkOut(type=LinkType.none)  # the target is hidden now: keep the text, drop the link
    return LinkOut(type=link.type, value=link.value)


def _order_id(n: Notification) -> int | None:
    value = (n.data or {}).get("order_id")
    return int(value) if value is not None else None


class InboxService:
    def __init__(self, session: AsyncSession, user: User) -> None:
        self.session = session
        self.user = user

    async def list(self, params: PageParams) -> InboxPage:
        """3 statements (+ the auth lookup): totals, page, link visibility."""
        total, unread = await repo.inbox_totals(self.session, self.user)
        rows = await repo.inbox_page(self.session, self.user, params.offset, params.page_size) if total else []
        visible = await visible_link_targets(self.session, (Link(n.link_type, n.link_value) for n, _ in rows))
        items = [
            NotificationOut(
                id=n.id, type=n.type, title=n.title, body=n.body, link=_inbox_link(n, self.user, visible),
                order_id=_order_id(n), sent_at=n.sent_at, is_read=read_at is not None, read_at=read_at,
            )
            for n, read_at in rows
        ]
        return InboxPage(items=items, page=params.page, page_size=params.page_size, total=total, unread_count=unread)

    async def unread_count(self) -> UnreadCountOut:
        return UnreadCountOut(unread_count=await repo.unread_count(self.session, self.user))

    async def mark_read(self, notification_id: int) -> UnreadCountOut:
        marked = await repo.mark_read(self.session, self.user, notification_id, clock.now_utc())
        if not marked and not await repo.is_in_inbox(self.session, self.user, notification_id):
            raise NotFound(ErrorCode.NOTIFICATION_NOT_FOUND)
        await self.session.commit()
        return await self.unread_count()

    async def mark_all_read(self) -> UnreadCountOut:
        await repo.mark_all_read(self.session, self.user, clock.now_utc())
        await self.session.commit()
        return UnreadCountOut(unread_count=0)


class PreferencesService:
    def __init__(self, session: AsyncSession, user: User) -> None:
        self.session = session
        self.user = user

    def get(self) -> PreferencesOut:
        u = self.user
        return PreferencesOut(notify_marketing=u.notify_marketing, notify_favorites=u.notify_favorites,
                              notify_cart_reminders=u.notify_cart_reminders)

    async def update(self, data: PreferencesUpdate) -> PreferencesOut:
        for key, value in data.model_dump(exclude_unset=True).items():
            if value is not None:
                setattr(self.user, key, value)
        await self.session.commit()
        return self.get()


# ------------------------------------------------------------------------------- admin


def admin_out(n: Notification, creator_name: str | None, user_name: str | None) -> AdminNotificationOut:
    return AdminNotificationOut(
        id=n.id, type=n.type, audience=n.target, user_id=n.user_id, user_name=user_name, title=n.title,
        body=n.body, link=LinkOut(type=n.link_type, value=n.link_value), status=n.status,
        scheduled_at=n.scheduled_at, sent_at=n.sent_at if n.status == NotificationStatus.sent else None,
        created_at=n.created_at, created_by_user_id=n.created_by_user_id, created_by_name=creator_name,
        recipients_count=n.recipients_count, success_count=n.success_count, failure_count=n.failure_count,
        deferred_by_quiet_hours=bool((n.data or {}).get("deferred_by_quiet_hours")),
    )


def check_schedule(when: datetime, now: datetime) -> None:
    if not now + SCHEDULE_MIN_AHEAD <= when <= now + SCHEDULE_MAX_AHEAD:
        raise ValidationFailed(
            ErrorCode.INVALID_SCHEDULE,
            details={"earliest": (now + SCHEDULE_MIN_AHEAD).isoformat(), "latest": (now + SCHEDULE_MAX_AHEAD).isoformat()},
        )


class AdminNotificationService:
    def __init__(self, session: AsyncSession, admin: User) -> None:
        self.session = session
        self.admin = admin

    async def _link(self, data: LinkIn | None) -> Link:
        if data is None:
            return NO_LINK
        return await validate_catalog_link(self.session, data.type, data.value)

    async def create(self, data: AdminNotificationCreate) -> AdminNotificationOut:
        now = clock.now_utc()
        if data.scheduled_at is not None:
            check_schedule(data.scheduled_at, now)
        recipient: User | None = None
        if data.audience == NotificationTarget.user:
            recipient = await self.session.get(User, data.user_id)
            if recipient is None:
                raise NotFound(ErrorCode.USER_NOT_FOUND)
        link = await self._link(data.link)
        type_ = NotificationType.marketing if recipient is None else NotificationType.message
        settings = await load_notification_settings(self.session) if type_ == NotificationType.marketing else None
        draft = Draft(type_, Text(data.title, data.body), recipient.id if recipient else None, link=link,
                      due=data.scheduled_at, created_by_user_id=self.admin.id)
        planned = plan(draft, now, settings)
        notification = Notification(**planned.row)
        self.session.add(notification)
        await self.session.commit()
        if planned.status == NotificationStatus.queued:
            kick_dispatch()
        return admin_out(notification, self.admin.name, recipient.name if recipient else None)

    async def list(self, params: PageParams, status: NotificationStatus | None, type_: NotificationType | None,
                   audience: NotificationTarget | None) -> Page[AdminNotificationOut]:
        """Campaigns (marketing + message) unless a type is given. 2 statements."""
        conditions: list[ColumnElement[bool]] = [
            Notification.type == type_ if type_ is not None else Notification.type.in_(CAMPAIGN_TYPES)
        ]
        if status is not None:
            conditions.append(Notification.status == status)
        if audience is not None:
            conditions.append(Notification.target == audience)
        total, rows = await repo.admin_page(self.session, params.offset, params.page_size, conditions)
        return Page(items=[admin_out(*row) for row in rows], page=params.page, page_size=params.page_size, total=total)

    async def get(self, notification_id: int) -> AdminNotificationOut:
        row = await repo.get_with_names(self.session, notification_id)
        if row is None:
            raise NotFound(ErrorCode.NOTIFICATION_NOT_FOUND)
        return admin_out(*row)

    async def _editable(self, notification_id: int) -> Notification:
        n = await repo.get_for_update(self.session, notification_id)
        if n is None:
            raise NotFound(ErrorCode.NOTIFICATION_NOT_FOUND)
        if n.status != NotificationStatus.scheduled or n.type not in CAMPAIGN_TYPES:
            raise Conflict(ErrorCode.NOTIFICATION_NOT_EDITABLE, details={"status": n.status.value})
        return n

    async def update(self, notification_id: int, data: AdminNotificationUpdate) -> AdminNotificationOut:
        n = await self._editable(notification_id)
        fields = data.model_fields_set
        if data.title is not None:
            n.title = data.title
        if data.body is not None:
            n.body = data.body
        if "link" in fields:
            link = await self._link(data.link)
            n.link_type, n.link_value = link.type, link.value
        if data.scheduled_at is not None:
            now = clock.now_utc()
            check_schedule(data.scheduled_at, now)
            when, deferred = data.scheduled_at, False
            if n.type == NotificationType.marketing:
                when, deferred = effective_schedule(n.type, when, await load_notification_settings(self.session))
            n.scheduled_at = when
            n.data = _with_deferred(n.data, deferred)
        await self.session.commit()
        return await self.get(n.id)

    async def cancel(self, notification_id: int) -> AdminNotificationOut:
        n = await self._editable(notification_id)
        n.status = NotificationStatus.cancelled
        await self.session.commit()
        return await self.get(n.id)

    async def send_now(self, notification_id: int) -> AdminNotificationOut:
        """Queue it right away, quiet hours or not."""
        n = await self._editable(notification_id)
        n.status = NotificationStatus.queued
        n.scheduled_at = clock.now_utc()
        n.data = _with_deferred(n.data, False)
        await self.session.commit()
        kick_dispatch()
        return await self.get(n.id)

    async def test(self, data: PushTestIn) -> PushTestOut:
        results = await send_test(self.session, self.admin.id, data.title, data.body)
        return PushTestOut(
            devices=len(results),
            results=[PushTestResultOut(token=r.token[:12] + "…", ok=r.ok, code=r.code, detail=r.detail) for r in results],
        )


def _with_deferred(data: dict[str, Any] | None, deferred: bool) -> dict[str, Any] | None:
    values = {k: v for k, v in (data or {}).items() if k != "deferred_by_quiet_hours"}
    if deferred:
        values["deferred_by_quiet_hours"] = True
    return values or None


__all__: Sequence[str] = ["AdminNotificationService", "InboxService", "PreferencesService"]
