"""The outbox dispatcher: due rows -> push. At-least-once delivery.

1. Claim due rows with FOR UPDATE SKIP LOCKED, set status 'sending' (sent_at = claim time), commit.
2. For each row: read the recipients' devices and badges, end that read transaction, push
   (batches of 500), delete dead tokens, retry UNAVAILABLE/INTERNAL once, store the counts and
   set 'sent'. No device: the row is just marked sent (inbox only).
3. A crash between 1 and 2 leaves the row in 'sending'; recover_stuck() re-queues it after
   10 minutes, so it may be pushed twice (the app ignores a notification_id it already showed).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock
from app.integrations.firebase.push import PushMessage, PushResult, PushSender, get_push_sender
from app.modules.notifications import repository as repo
from app.modules.notifications.enums import NotificationType
from app.modules.notifications.models import Notification
from app.modules.notifications.runtime import notification_session
from app.shared.enums import ADMIN_LINK_TYPES, LinkType

logger = logging.getLogger(__name__)

CLAIM_BATCH = 20
FANOUT_BATCH = 500
STUCK_AFTER = timedelta(minutes=10)
ORDER_CHANNEL_TYPES = frozenset({NotificationType.order_update, NotificationType.admin_alert})


@dataclass(frozen=True)
class Claimed:
    id: int
    type: NotificationType
    user_id: int | None
    title: str
    body: str
    link_type: LinkType
    link_value: str | None
    order_id: int | None

    @classmethod
    def of(cls, row: Notification) -> "Claimed":
        order_id = (row.data or {}).get("order_id")
        return cls(row.id, row.type, row.user_id, row.title, row.body, row.link_type, row.link_value,
                   int(order_id) if order_id is not None else None)


@dataclass
class Outcome:
    recipients: int = 0
    success: int = 0
    failure: int = 0

    def add(self, results: Sequence[PushResult]) -> None:
        self.recipients += len(results)
        self.success += sum(1 for r in results if r.ok)
        self.failure += sum(1 for r in results if not r.ok)


def channel_for(type_: NotificationType) -> str:
    return "orders" if type_ in ORDER_CHANNEL_TYPES else "marketing"


def payload_data(n: Claimed) -> dict[str, str]:
    """FCM data (strings only). admin_* links only ever leave in admin alerts."""
    link_type, link_value = n.link_type, n.link_value
    if link_type in ADMIN_LINK_TYPES and n.type != NotificationType.admin_alert:
        link_type, link_value = LinkType.none, None
    return {
        "notification_id": str(n.id),
        "type": n.type.value,
        "link_type": link_type.value,
        "link_value": link_value or "",
        "order_id": str(n.order_id) if n.order_id is not None else "",
    }


def messages_for(n: Claimed, devices: Sequence[Any], badges: dict[int, int]) -> list[PushMessage]:
    data = payload_data(n)
    channel = channel_for(n.type)
    # The row being sent is not counted yet (status 'sending'), hence + 1.
    return [PushMessage(d.fcm_token, n.title, n.body, data, channel, badges.get(d.user_id, 0) + 1) for d in devices]


async def send_with_retry(sender: PushSender, messages: Sequence[PushMessage]) -> list[PushResult]:
    """Send; messages failing with UNAVAILABLE / INTERNAL are retried once."""
    if not messages:
        return []
    results = await sender.send(messages)
    retry = [i for i, r in enumerate(results) if r.retryable]
    if retry:
        again = await sender.send([messages[i] for i in retry])
        for i, result in zip(retry, again, strict=True):
            results[i] = result
    return results


async def _push(session: AsyncSession, sender: PushSender, messages: list[PushMessage]) -> list[PushResult]:
    """End the read transaction, push, then delete dead tokens (own small transaction)."""
    await session.commit()
    results = await send_with_retry(sender, messages)
    dead = [r.token for r in results if r.invalid_token]
    if dead:
        await repo.delete_devices(session, dead)
        await session.commit()
        logger.info("Deleted %d invalid FCM tokens", len(dead))
    return results


async def deliver(n: Claimed, sender: PushSender | None = None) -> Outcome:
    sender = sender or get_push_sender()
    outcome = Outcome()
    async with notification_session() as session:
        if n.user_id is not None:
            devices = await repo.user_devices(session, n.user_id, n.type)
            if devices:
                badges = await repo.unread_counts(session, [n.user_id])
                outcome.add(await _push(session, sender, messages_for(n, devices, badges)))
        else:
            after = 0
            while True:
                devices = await repo.broadcast_devices(session, n.type, after, FANOUT_BATCH)
                if not devices:
                    break
                after = devices[-1].id
                badges = await repo.unread_counts(session, {d.user_id for d in devices})
                outcome.add(await _push(session, sender, messages_for(n, devices, badges)))
                if len(devices) < FANOUT_BATCH:
                    break
        await repo.finish(session, n.id, clock.now_utc(), outcome.recipients, outcome.success, outcome.failure)
        await session.commit()
    return outcome


async def claim(limit: int = CLAIM_BATCH) -> list[Claimed]:
    async with notification_session() as session:
        now = clock.now_utc()
        rows = await repo.claim_due(session, now, limit)
        claimed = [Claimed.of(r) for r in rows]
        if claimed:
            await repo.set_sending(session, [c.id for c in claimed], now)
        await session.commit()
    return claimed


async def dispatch_due(sender: PushSender | None = None, max_rows: int = 1000) -> int:
    """Send everything due (up to max_rows per call). Returns how many rows were processed."""
    done = 0
    while done < max_rows:
        batch = await claim(min(CLAIM_BATCH, max_rows - done))
        if not batch:
            break
        for n in batch:
            try:
                await deliver(n, sender)
            except Exception:
                # Stays 'sending'; recover_stuck() re-queues it after STUCK_AFTER.
                logger.exception("Delivering notification %s failed", n.id)
        done += len(batch)
    return done


async def recover_stuck() -> int:
    async with notification_session() as session:
        count = await repo.requeue_stuck(session, clock.now_utc() - STUCK_AFTER)
        await session.commit()
    if count:
        logger.warning("Re-queued %d notifications stuck in 'sending'", count)
    return count


async def send_test(session: AsyncSession, user_id: int, title: str, body: str,
                    sender: PushSender | None = None) -> list[PushResult]:
    """POST /admin/notifications/test: the admin's own devices, no inbox row."""
    tokens = await repo.devices_of(session, user_id)
    data = {"notification_id": "", "type": NotificationType.message.value, "link_type": LinkType.none.value,
            "link_value": "", "order_id": "", "test": "1"}
    messages = [PushMessage(t, title, body, data, "orders") for t in tokens]
    return await _push(session, sender or get_push_sender(), messages)
