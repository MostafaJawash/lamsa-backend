"""The outbox dispatcher: payloads, badges, token hygiene, retries, fan-out, claims, recovery."""

from datetime import datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db.session import CONNECT_ARGS
from app.integrations.firebase import push
from app.modules.notifications import dispatcher, templates
from app.modules.notifications.dispatcher import claim, dispatch_due, recover_stuck
from app.modules.notifications.enums import NotificationType
from app.modules.notifications.models import Notification
from app.modules.notifications.outbox import Draft, add_drafts
from app.modules.users.models import UserDevice
from cart_fixtures import build_shop
from conftest import FakeClock, FakePushSender, bearer, register
from notification_helpers import ADMIN_NOTIFICATIONS, add_device, me, rows, set_user
from order_helpers import order_from_cart

pytestmark = pytest.mark.anyio

NOON = datetime(2026, 7, 1, 10, 0)  # 12:00 in Berlin


async def _message(session: AsyncSession, user_id: int | None, type_: NotificationType = NotificationType.message,
                   **extra: Any) -> None:
    await add_drafts(session, [Draft(type_, templates.Text("Hallo", "Body"), user_id, **extra)])
    await session.commit()


async def test_order_update_push_payload_channel_and_badge(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, db_session: AsyncSession, pushes: FakePushSender
) -> None:
    shop = await build_shop(client, admin_headers, db_session)
    await add_device(client, customer_headers, "customer-phone")
    await add_device(client, customer_headers, "customer-tablet", "ios")
    await add_device(client, admin_headers, "admin-phone")
    # Lipstick red 5 -> 4 stays above the low-stock threshold: exactly two rows.
    order = await order_from_cart(client, customer_headers, [(shop["lip"]["id"], shop["red"]["id"], 1)], method="pickup")

    assert await dispatch_due() == 2  # the customer's update and the admin alert
    by_token = {m.token: m for m in pushes.sent}
    customer = by_token["customer-phone"]
    assert customer.title == f"تم استلام طلبك #{order['id']} ✅"
    assert customer.channel == "orders" and customer.badge == 1
    assert customer.data == {"notification_id": customer.data["notification_id"], "type": "order_update",
                             "link_type": "order", "link_value": str(order["id"]), "order_id": str(order["id"])}
    assert by_token["customer-tablet"].data == customer.data
    admin = by_token["admin-phone"]
    assert (admin.data["type"], admin.data["link_type"], admin.data["link_value"]) == ("admin_alert", "admin_order", str(order["id"]))

    update = (await rows(db_session, Notification.type == "order_update"))[0]
    assert (update.status, update.recipients_count, update.success_count, update.failure_count) == ("sent", 2, 2, 0)
    assert update.sent_at is not None and str(update.id) == customer.data["notification_id"]
    assert await dispatch_due() == 0  # nothing sent twice


async def test_dead_tokens_are_deleted_and_transient_errors_retried_once(
    client: AsyncClient, customer_headers: dict, db_session: AsyncSession, pushes: FakePushSender
) -> None:
    user_id = await me(client, customer_headers)
    for token in ("dead", "wrong-sender", "bad-arg", "flaky", "broken", "good"):
        await add_device(client, customer_headers, token)
    pushes.codes = {"dead": [push.UNREGISTERED], "wrong-sender": [push.SENDER_ID_MISMATCH],
                    "bad-arg": [push.INVALID_ARGUMENT], "flaky": [push.UNAVAILABLE, push.OK],
                    "broken": [push.INTERNAL, push.INTERNAL]}
    await _message(db_session, user_id)

    assert await dispatch_due() == 1
    assert [sorted(m.token for m in call) for call in pushes.calls] == [
        ["bad-arg", "broken", "dead", "flaky", "good", "wrong-sender"], ["broken", "flaky"]]  # one retry, once
    (n,) = await rows(db_session)
    assert (n.status, n.recipients_count, n.success_count, n.failure_count) == ("sent", 6, 2, 4)
    remaining = set(await db_session.scalars(select(UserDevice.fcm_token)))
    assert remaining == {"flaky", "broken", "good"}


async def test_no_devices_means_inbox_only(client: AsyncClient, customer_headers: dict, db_session: AsyncSession,
                                           pushes: FakePushSender) -> None:
    await _message(db_session, await me(client, customer_headers))
    assert await dispatch_due() == 1
    (n,) = await rows(db_session)
    assert (n.status, n.recipients_count, n.success_count, n.failure_count) == ("sent", 0, 0, 0)
    assert pushes.sent == []


async def test_broadcast_fans_out_in_batches_respecting_preferences(
    client: AsyncClient, admin_headers: dict, db_session: AsyncSession, pushes: FakePushSender,
    monkeypatch: pytest.MonkeyPatch, frozen_clock: FakeClock,
) -> None:
    frozen_clock.now = NOON
    monkeypatch.setattr(dispatcher, "FANOUT_BATCH", 2)
    users = []
    for n in range(40, 45):
        data = await register(client, n, name=f"User {n}")
        users.append(data["user"]["id"])
        await add_device(client, bearer(data["access_token"]), f"token-{n}")
    await set_user(db_session, users[1], notify_marketing=False)
    await set_user(db_session, users[2], is_active=False)

    created = await client.post(ADMIN_NOTIFICATIONS, json={"audience": "all", "title": "تخفيضات", "body": "٢٠٪"}, headers=admin_headers)
    assert created.status_code == 201, created.text
    assert await dispatch_due() == 1
    assert [[m.token for m in call] for call in pushes.calls] == [["token-40", "token-43"], ["token-44"]]
    assert all(m.channel == "marketing" and m.data["type"] == "marketing" and m.badge == 1 for m in pushes.sent)
    (n,) = await rows(db_session)
    assert (n.recipients_count, n.success_count) == (3, 3)


async def test_scheduled_rows_wait_until_due(client: AsyncClient, customer_headers: dict, db_session: AsyncSession,
                                             frozen_clock: FakeClock, pushes: FakePushSender) -> None:
    frozen_clock.now = NOON
    user_id = await me(client, customer_headers)
    await add_device(client, customer_headers, "phone")
    await _message(db_session, user_id, due=NOON + timedelta(hours=2))
    assert (await rows(db_session))[0].status == "scheduled"
    assert await dispatch_due() == 0
    frozen_clock.advance(hours=2)
    assert await dispatch_due() == 1 and [m.token for m in pushes.sent] == ["phone"]


async def test_claimed_rows_are_not_claimed_again_and_stuck_ones_come_back(
    client: AsyncClient, customer_headers: dict, db_session: AsyncSession, frozen_clock: FakeClock, pushes: FakePushSender
) -> None:
    frozen_clock.now = NOON
    await add_device(client, customer_headers, "phone")
    await _message(db_session, await me(client, customer_headers))
    assert [c.title for c in await claim()] == ["Hallo"]  # ... and then the process "crashes"
    assert await claim() == []
    (n,) = await rows(db_session)
    assert (n.status, n.sent_at) == ("sending", NOON)

    frozen_clock.advance(minutes=9)
    assert await recover_stuck() == 0
    frozen_clock.advance(minutes=2)
    assert await recover_stuck() == 1
    assert (await rows(db_session))[0].status == "queued"
    assert await dispatch_due() == 1 and len(pushes.sent) == 1
    assert (await rows(db_session))[0].status == "sent"


async def test_a_failing_row_does_not_stop_the_others(
    client: AsyncClient, customer_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = await me(client, customer_headers)
    await _message(db_session, user_id)
    await _message(db_session, user_id)
    real_deliver = dispatcher.deliver
    calls: list[int] = []

    async def flaky_deliver(n: dispatcher.Claimed, sender: Any = None) -> dispatcher.Outcome:
        calls.append(n.id)
        if len(calls) == 1:
            raise RuntimeError("network down")
        return await real_deliver(n, sender)

    monkeypatch.setattr(dispatcher, "deliver", flaky_deliver)
    assert await dispatch_due() == 2
    assert [n.status for n in await rows(db_session)] == ["sending", "sent"]  # the first waits for recovery


async def test_test_push_goes_to_own_devices_without_a_row(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, db_session: AsyncSession, pushes: FakePushSender
) -> None:
    await add_device(client, admin_headers, "admin-phone-123456789")
    await add_device(client, admin_headers, "admin-old-phone-000000")
    await add_device(client, customer_headers, "customer-phone")
    pushes.codes = {"admin-old-phone-000000": [push.UNREGISTERED]}
    response = await client.post(f"{ADMIN_NOTIFICATIONS}/test", json={"title": "Test", "body": "Hi"}, headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["devices"] == 2
    assert [(r["token"], r["ok"], r["code"]) for r in body["results"]] == [
        ("admin-phone-…", True, "OK"), ("admin-old-ph…", False, "UNREGISTERED")]
    assert [m.token for m in pushes.sent] == ["admin-phone-123456789", "admin-old-phone-000000"]
    assert pushes.sent[0].data["notification_id"] == "" and pushes.sent[0].data["test"] == "1"
    assert await rows(db_session) == []
    assert "admin-old-phone-000000" not in set(await db_session.scalars(select(UserDevice.fcm_token)))
    assert (await client.post(f"{ADMIN_NOTIFICATIONS}/test", headers=customer_headers)).status_code == 403


# ---------------------------------------------------------- SKIP LOCKED (real commits)


async def test_skip_locked_with_two_real_connections(migrated_db: None) -> None:
    """A row locked by one dispatcher is invisible to another one (committed data, cleaned up)."""
    assert settings.DB_NAME.endswith("_test")
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool, connect_args=CONNECT_ARGS)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    marker = "skip-locked-test"
    try:
        async with maker() as setup:
            await setup.execute(insert(Notification).values([
                {"title": marker, "body": "1", "target": "all", "type": "marketing", "status": "queued"},
                {"title": marker, "body": "2", "target": "all", "type": "marketing", "status": "queued"},
            ]))
            await setup.commit()
        async with maker() as first, maker() as second:
            mine = (await first.scalars(select(Notification).where(Notification.title == marker).order_by(Notification.id)
                                        .limit(1).with_for_update(skip_locked=True))).all()
            theirs = (await second.scalars(select(Notification).where(Notification.title == marker).order_by(Notification.id)
                                           .with_for_update(skip_locked=True))).all()
            assert len(mine) == 1 and [n.body for n in theirs] == ["2"]
            await first.rollback()
            await second.rollback()
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(Notification).where(Notification.title == marker))
            assert (await conn.execute(text("SELECT COUNT(*) FROM notifications WHERE title = :t"), {"t": marker})).scalar() == 0
        await engine.dispose()
