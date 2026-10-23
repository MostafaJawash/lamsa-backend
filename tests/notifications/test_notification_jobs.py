"""cart_changed_at on every cart write, cart reminders, cleanup and the worker loop."""

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db.session import CONNECT_ARGS
from app.modules.notifications import jobs
from app.modules.notifications.models import Notification, UserAlertLog
from app.modules.orders.models import Order
from app.modules.products.models import Product
from app.modules.users.models import User
from app.workers.runner import Job, Worker
from cart_fixtures import CART, FAVORITES, add, build_shop
from catalog_helpers import ADMIN, ok
from conftest import FakeClock, bearer, register
from notification_helpers import PREFERENCES, me, rows, set_user
from order_helpers import order_from_cart

pytestmark = pytest.mark.anyio

NOON = datetime(2026, 7, 1, 10, 0)  # 12:00 in Berlin


@pytest.fixture
async def shop(client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession) -> dict[str, Any]:
    return await build_shop(client, admin_headers, db_session)


async def changed_at(session: AsyncSession, user_id: int) -> datetime | None:
    value: datetime | None = await session.scalar(
        select(User.cart_changed_at).where(User.id == user_id).execution_options(populate_existing=True)
    )
    return value


# ------------------------------------------------------------------------ cart_changed_at


async def test_every_cart_write_sets_cart_changed_at(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict, db_session: AsyncSession,
    frozen_clock: FakeClock,
) -> None:
    frozen_clock.now = NOON
    user_id = await me(client, customer_headers)
    cream, lip, red = shop["simple"]["id"], shop["lip"]["id"], shop["red"]["id"]
    assert await changed_at(db_session, user_id) is None

    async def step(label: str, response: Any, status: int = 200) -> None:
        assert response.status_code == status, (label, response.text)
        assert await changed_at(db_session, user_id) == frozen_clock.now, label
        frozen_clock.advance(minutes=1)

    await step("add", await add(client, customer_headers, cream), 201)
    line = (await client.get(CART, headers=customer_headers)).json()["lines"][0]["id"]
    await step("patch", await client.patch(f"{CART}/items/{line}", json={"quantity": 2}, headers=customer_headers))
    await step("delete line", await client.delete(f"{CART}/items/{line}", headers=customer_headers))
    await step("merge", await client.post(f"{CART}/merge", json={"items": [{"product_id": cream, "quantity": 1}]},
                                          headers=customer_headers))
    await step("clear", await client.delete(CART, headers=customer_headers))
    assert (await client.put(f"{FAVORITES}/{lip}", headers=customer_headers)).status_code == 204
    await step("move to cart", await client.post(f"{FAVORITES}/{lip}/move-to-cart", json={"variant_id": red},
                                                 headers=customer_headers))
    await db_session.execute(update(Product).where(Product.id == lip).values(is_active=False))
    await db_session.commit()
    await step("resolve issues", await client.post(f"{CART}/resolve-issues", headers=customer_headers))
    await db_session.execute(update(Product).where(Product.id == lip).values(is_active=True))
    await db_session.commit()
    ok(await add(client, customer_headers, cream), 201)
    frozen_clock.advance(minutes=1)
    await order_from_cart(client, customer_headers, [], method="pickup")
    assert await changed_at(db_session, user_id) == frozen_clock.now, "cart cleanup after placing an order"


async def test_reads_and_no_op_resolve_do_not_touch(
    client: AsyncClient, customer_headers: dict, shop: dict, db_session: AsyncSession, frozen_clock: FakeClock
) -> None:
    frozen_clock.now = NOON
    user_id = await me(client, customer_headers)
    ok(await add(client, customer_headers, shop["simple"]["id"]), 201)
    frozen_clock.advance(minutes=5)
    ok(await client.get(CART, headers=customer_headers))
    ok(await client.post(f"{CART}/resolve-issues", headers=customer_headers))  # nothing to resolve
    assert await changed_at(db_session, user_id) == NOON


# -------------------------------------------------------------------------- cart reminders


Line = int | tuple[int, int]  # a simple product id, or (product id, variant id)


async def _shopper(client: AsyncClient, n: int, *lines: Line) -> tuple[dict[str, str], int]:
    data = await register(client, n, name=f"Shopper {n}")
    headers = bearer(data["access_token"])
    for line in lines:
        product_id, variant_id = line if isinstance(line, tuple) else (line, None)
        ok(await add(client, headers, product_id, variant_id), 201)
    return headers, data["user"]["id"]


async def test_cart_reminder_once_per_cart_change(
    client: AsyncClient, shop: dict, db_session: AsyncSession, frozen_clock: FakeClock
) -> None:
    frozen_clock.now = NOON
    headers, user_id = await _shopper(client, 30, shop["simple"]["id"], (shop["perfume"]["id"], shop["p50"]["id"]))
    frozen_clock.advance(hours=23, minutes=59)
    assert await jobs.cart_reminders() == 0  # too early
    frozen_clock.advance(minutes=1)
    assert await jobs.cart_reminders() == 1
    (reminder,) = await rows(db_session, Notification.type == "cart_reminder")
    assert reminder.user_id == user_id and reminder.status == "queued" and reminder.link_type == "none"
    assert "Cream" in reminder.body and "1" in reminder.body  # the first product and one other line
    assert await jobs.cart_reminders() == 0  # once per cart change

    frozen_clock.advance(minutes=1)
    ok(await add(client, headers, shop["lip"]["id"], shop["red"]["id"]), 201)  # a new change
    frozen_clock.advance(hours=24)
    assert await jobs.cart_reminders() == 1
    assert len(await rows(db_session, Notification.type == "cart_reminder")) == 2
    log = await db_session.scalar(select(func.count()).select_from(UserAlertLog).where(UserAlertLog.kind == "cart_reminder"))
    assert log == 2


async def test_cart_reminder_skips(
    client: AsyncClient, admin_headers: dict, shop: dict, db_session: AsyncSession, frozen_clock: FakeClock
) -> None:
    frozen_clock.now = NOON
    cream, lip = shop["simple"]["id"], (shop["lip"]["id"], shop["red"]["id"])
    off_headers, _ = await _shopper(client, 31, cream)
    ok(await client.patch(PREFERENCES, json={"notify_cart_reminders": False}, headers=off_headers))
    _, inactive = await _shopper(client, 32, cream)
    await set_user(db_session, inactive, is_active=False)
    _, unavailable = await _shopper(client, 33, lip)
    _, ordered = await _shopper(client, 34, cream)
    _, first_available = await _shopper(client, 35, lip, cream)
    ok(await client.patch(f"{ADMIN}/products/{lip[0]}", json={"is_active": False}, headers=admin_headers))
    # An order placed after the last cart change: no reminder.
    await db_session.execute(insert(Order).values(
        user_id=ordered, customer_name="x", customer_phone="+4915100000000", delivery_method_code="pickup",
        delivery_method_name="Pickup", payment_method="cod", payment_status="pending", status="pending",
        subtotal=1, shipping_fee=0, total=1, created_at=NOON + timedelta(hours=1), updated_at=NOON + timedelta(hours=1)))
    await db_session.commit()

    frozen_clock.advance(hours=24)
    assert await jobs.cart_reminders() == 1
    (reminder,) = await rows(db_session, Notification.type == "cart_reminder")
    assert reminder.user_id == first_available
    assert reminder.body.startswith("Cream") and "1" in reminder.body  # the hidden lipstick still counts as a line
    assert unavailable not in {reminder.user_id}


async def test_cart_reminder_window_and_quiet_hours(
    client: AsyncClient, shop: dict, db_session: AsyncSession, frozen_clock: FakeClock
) -> None:
    frozen_clock.now = datetime(2026, 7, 1, 19, 30)  # 21:30 in Berlin
    await _shopper(client, 36, shop["simple"]["id"])
    frozen_clock.advance(hours=24 + 72, minutes=1)  # left alone too long
    assert await jobs.cart_reminders() == 0

    frozen_clock.now = datetime(2026, 7, 10, 19, 30)
    await _shopper(client, 37, shop["simple"]["id"])
    frozen_clock.advance(hours=24)  # 21:30 Berlin the next day
    assert await jobs.cart_reminders() == 1
    (reminder,) = await rows(db_session, Notification.type == "cart_reminder")
    assert (reminder.status, reminder.scheduled_at) == ("scheduled", datetime(2026, 7, 12, 7, 0))


async def test_cart_reminders_run_on_one_worker_only(
    client: AsyncClient, shop: dict, db_session: AsyncSession, frozen_clock: FakeClock
) -> None:
    frozen_clock.now = NOON
    await _shopper(client, 38, shop["simple"]["id"])
    frozen_clock.advance(hours=25)
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool, connect_args=CONNECT_ARGS)
    try:
        async with engine.connect() as other:
            assert (await other.execute(text("SELECT GET_LOCK(:n, 0)"), {"n": jobs.CART_REMINDER_LOCK})).scalar() == 1
            assert await jobs.cart_reminders() == 0
            await other.execute(text("SELECT RELEASE_LOCK(:n)"), {"n": jobs.CART_REMINDER_LOCK})
    finally:
        await engine.dispose()
    assert await jobs.cart_reminders() == 1


# --------------------------------------------------------------------------------- cleanup


async def test_cleanup_deletes_old_rows_only(
    client: AsyncClient, customer_headers: dict, db_session: AsyncSession, frozen_clock: FakeClock
) -> None:
    frozen_clock.now = NOON
    user_id = await me(client, customer_headers)
    old, recent = NOON - timedelta(days=181), NOON - timedelta(days=179)
    base = {"body": "b", "target": "user", "type": "message", "user_id": user_id, "sent_at": None, "scheduled_at": None}
    await db_session.execute(insert(Notification).values([
        {**base, "title": "old sent", "status": "sent", "sent_at": old, "created_at": old},
        {**base, "title": "recent sent", "status": "sent", "sent_at": recent, "created_at": recent},
        {**base, "title": "old cancelled", "status": "cancelled", "created_at": old},
        {**base, "title": "old but scheduled", "status": "scheduled", "scheduled_at": NOON + timedelta(days=1), "created_at": old},
    ]))
    await db_session.execute(insert(UserAlertLog).values([
        {"user_id": user_id, "kind": "favorite_price_drop", "ref_id": 1, "created_at": old},
        {"user_id": user_id, "kind": "favorite_price_drop", "ref_id": 2, "created_at": recent},
    ]))
    await db_session.commit()
    assert await jobs.cleanup() == (2, 1)
    assert [n.title for n in await rows(db_session)] == ["recent sent", "old but scheduled"]
    assert list(await db_session.scalars(select(UserAlertLog.ref_id))) == [2]


# ---------------------------------------------------------------------------------- worker


async def test_worker_survives_failing_jobs_and_stops_gracefully() -> None:
    calls: list[str] = []
    worker: Worker

    async def boom() -> None:
        calls.append("boom")
        raise RuntimeError("job failed")

    async def count() -> None:
        calls.append("count")
        if calls.count("count") == 2:
            worker.stop()

    worker = Worker([Job("boom", 0.01, boom), Job("count", 0.01, count)])
    await asyncio.wait_for(worker.run(), timeout=5)
    assert calls == ["boom", "count", "boom", "count"]


def test_worker_entry_point_imports() -> None:
    import app.workers.run as entry
    from app.workers.runner import default_jobs

    assert callable(entry.main)
    assert [(j.name, j.every_seconds) for j in default_jobs()] == [
        ("dispatch", 30), ("recover_stuck", 300), ("cart_reminders", 900), ("cleanup", 86400)]
