"""Creating notification rows (the outbox). Handlers, jobs and the admin service go through here.

Rules applied to every row:
- target 'user' has a user_id, target 'all' has none (MySQL cannot CHECK this, see the model);
- recipient link safety: admin_* links only for admins, an order link only for its owner;
- quiet hours: marketing / favorite_* / cart_reminder due inside them are deferred to their end
  (status 'scheduled'); everything else is 'queued' for immediate sending.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock
from app.modules.notifications import repository as repo
from app.modules.notifications.enums import (
    QUIET_HOURS_TYPES,
    NotificationStatus,
    NotificationTarget,
    NotificationType,
)
from app.modules.notifications.links import Link, check_recipient_link
from app.modules.notifications.quiet_hours import effective_schedule
from app.modules.notifications.templates import Text
from app.modules.settings.service import NotificationSettings, load_notification_settings
from app.shared.enums import LinkType

NO_LINK = Link(LinkType.none, None)


@dataclass(frozen=True)
class Draft:
    type: NotificationType
    text: Text
    user_id: int | None  # None: everyone (target 'all')
    link: Link = NO_LINK
    order_id: int | None = None  # for order_update / order admin alerts
    order_owner_id: int | None = None  # required for an `order` link
    due: datetime | None = None  # None: now
    created_by_user_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Planned:
    row: dict[str, Any]
    scheduled_at: datetime
    deferred_by_quiet_hours: bool
    status: NotificationStatus


def plan(draft: Draft, now: datetime, settings: NotificationSettings | None, *, admin_ids: Collection[int] = ()) -> Planned:
    target = NotificationTarget.all if draft.user_id is None else NotificationTarget.user
    check_recipient_link(
        draft.link,
        recipient_is_admin=draft.user_id is not None and draft.user_id in admin_ids,
        order_owner_id=draft.order_owner_id,
        recipient_id=draft.user_id,
    )
    due = draft.due or now
    deferred = False
    if draft.type in QUIET_HOURS_TYPES:
        if settings is None:
            raise ValueError("quiet-hours types need the notification settings")
        due, deferred = effective_schedule(draft.type, due, settings)
    status = NotificationStatus.scheduled if due > now else NotificationStatus.queued
    data: dict[str, Any] = dict(draft.extra)
    if draft.order_id is not None:
        data["order_id"] = draft.order_id
    if deferred:
        data["deferred_by_quiet_hours"] = True
    row = {
        "title": draft.text.title,
        "body": draft.text.body,
        "target": target,
        "type": draft.type,
        "status": status,
        "scheduled_at": due,
        "created_by_user_id": draft.created_by_user_id,
        "user_id": draft.user_id,
        "link_type": draft.link.type,
        "link_value": draft.link.value,
        "data": data or None,
        "created_at": now,  # the injectable clock, not the database clock
    }
    return Planned(row, due, deferred, status)


async def add_drafts(
    session: AsyncSession,
    drafts: Sequence[Draft],
    *,
    admin_ids: Collection[int] = (),
    settings: NotificationSettings | None = None,
) -> list[Planned]:
    """Insert drafts in ONE statement (plus one settings read when quiet hours matter and no
    settings were passed). The caller commits, then calls runtime.kick_dispatch()."""
    if not drafts:
        return []
    if settings is None and any(d.type in QUIET_HOURS_TYPES for d in drafts):
        settings = await load_notification_settings(session)
    now = clock.now_utc()
    planned = [plan(d, now, settings, admin_ids=admin_ids) for d in drafts]
    await repo.insert_rows(session, [p.row for p in planned])
    return planned
