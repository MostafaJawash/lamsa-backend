"""No events (and so no notifications) after a rollback; statement budgets of the endpoints and
the event handlers (measured; the numbers are reported in the README)."""

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications import handlers, templates
from app.modules.notifications.dispatcher import dispatch_due
from app.modules.notifications.enums import NotificationType
from app.modules.notifications.links import Link
from app.modules.notifications.models import Notification
from app.modules.notifications.outbox import Draft, add_drafts
from app.shared import events
from app.shared.enums import LinkType
from cart_fixtures import FAVORITES, build_shop
from catalog_helpers import ADMIN, ok, png
from conftest import bearer, register
from notification_helpers import ADMIN_BANNERS, ADMIN_NOTIFICATIONS, BANNERS, NOTIFICATIONS, add_device, me, rows

pytestmark = pytest.mark.anyio


@pytest.fixture
async def shop(client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession) -> dict[str, Any]:
    return await build_shop(client, admin_headers, db_session)


@pytest.fixture
def recorded() -> Iterator[list[events.DomainEvent]]:
    seen: list[events.DomainEvent] = []

    async def record(event: events.DomainEvent) -> None:
        seen.append(event)

    events.subscribe(events.DomainEvent, record)
    yield seen
    events.unsubscribe(record)


# ------------------------------------------------------------------------------ rollback


async def test_no_product_events_or_notifications_when_the_commit_fails(
    client: AsyncClient, admin_headers: dict, shop: dict, db_session: AsyncSession, recorded: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cream = shop["simple"]["id"]
    fan = bearer((await register(client, 75, name="Fan"))["access_token"])
    assert (await client.put(f"{FAVORITES}/{cream}", headers=fan)).status_code == 204
    adjust = f"{ADMIN}/products/{cream}/stock-adjust"
    ok(await client.post(adjust, json={"delta": -5}, headers=admin_headers))
    assert [type(e).__name__ for e in recorded] == ["StockOut"]
    recorded.clear()
    before = len(await rows(db_session))

    async def failing_commit() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db_session, "commit", failing_commit)
    # Back in stock (ProductBackInStock stashed) and a price drop: both rolled back.
    assert (await client.post(adjust, json={"delta": 3}, headers=admin_headers)).status_code == 500
    assert (await client.patch(f"{ADMIN}/products/{cream}", json={"sale_price": "5.00"}, headers=admin_headers)).status_code == 500
    monkeypatch.undo()
    assert recorded == []
    assert len(await rows(db_session)) == before
    assert db_session.info.get(events.STASH_KEY) is None


async def test_a_plain_rollback_clears_stashed_events(db_session: AsyncSession, recorded: list) -> None:
    await db_session.execute(select(1))  # events are only ever stashed inside a transaction
    events.stash(db_session, events.ProductBackInStock(product_id=1))
    await db_session.rollback()
    await events.commit_and_publish(db_session)
    assert recorded == []


async def test_failed_commit_drops_buffered_and_stashed_events(db_session: AsyncSession, recorded: list,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = events.EventBuffer()
    buffer.add(events.OrderPlaced(order_id=1, user_id=1, total="1.00"))
    events.stash(db_session, events.StockOut(product_id=1, variant_id=None))

    async def failing_commit() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db_session, "commit", failing_commit)
    with pytest.raises(RuntimeError):
        await events.commit_and_publish(db_session, buffer)
    monkeypatch.undo()
    assert buffer.events == [] and db_session.info.get(events.STASH_KEY) is None
    await events.commit_and_publish(db_session, buffer)
    assert recorded == []


# ------------------------------------------------------------------------- read budgets


async def test_read_budgets(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict, db_session: AsyncSession,
    count_queries: Any,
) -> None:
    customer = await me(client, customer_headers)
    cream = shop["simple"]
    await add_drafts(db_session, [
        Draft(NotificationType.marketing, templates.Text("p", "b"), None, link=Link(LinkType.product, cream["slug"])),
        Draft(NotificationType.message, templates.Text("c", "b"), customer, link=Link(LinkType.category, shop["category"]["slug"])),
        Draft(NotificationType.message, templates.Text("b", "b"), customer, link=Link(LinkType.brand, shop["brand"]["slug"])),
    ])
    await db_session.commit()
    await dispatch_due()
    for link in ({"link_type": "product", "link_value": cream["slug"]}, {"link_type": "brand", "link_value": shop["brand"]["slug"]}):
        ok(await client.post(ADMIN_BANNERS, data={"title": "x", **link}, files={"image": ("b.png", png(), "image/png")},
                             headers=admin_headers), 201)
    ok(await client.post(ADMIN_NOTIFICATIONS, json={"audience": "all", "title": "x", "body": "y"}, headers=admin_headers), 201)

    measured = {}
    for name, method, path, headers, budget in (
        ("GET /notifications", "get", NOTIFICATIONS, customer_headers, 4),
        ("GET /notifications/unread-count", "get", f"{NOTIFICATIONS}/unread-count", customer_headers, 3),
        ("GET /banners", "get", BANNERS, {}, 3),
        ("GET /admin/notifications", "get", ADMIN_NOTIFICATIONS, admin_headers, 4),
    ):
        with count_queries() as counter:
            ok(await client.request(method, path, headers=headers))
        measured[name] = counter.count
        assert counter.count <= budget, (name, counter.statements)
    print("\nREAD BUDGETS", measured)
    assert measured == {"GET /notifications": 4, "GET /notifications/unread-count": 2, "GET /banners": 2,
                        "GET /admin/notifications": 3}


# --------------------------------------------------------------------- handler budgets


async def test_handler_statement_counts(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict, db_session: AsyncSession,
    count_queries: Any,
) -> None:
    """Statements per handler run (commit / savepoint bookkeeping excluded)."""
    customer = await me(client, customer_headers)
    cream, pink = shop["simple"], shop["pink"]
    for n in (76, 77, 78):  # three fans: the favorites handlers stay batched
        fan = bearer((await register(client, n, name=f"Fan {n}"))["access_token"])
        assert (await client.put(f"{FAVORITES}/{cream['id']}", headers=fan)).status_code == 204

    cases: list[tuple[str, events.DomainEvent]] = [
        ("OrderPlaced", events.OrderPlaced(order_id=1, user_id=customer, total="8.00", items_count=1,
                                           delivery_method_name="Pickup", is_pickup=True)),
        ("OrderStatusChanged", events.OrderStatusChanged(order_id=1, user_id=customer, from_status="pending",
                                                         to_status="preparing", changed_by_user_id=None)),
        ("OrderCancelled (by admin)", events.OrderCancelled(order_id=1, user_id=customer, by="admin", stock_restored=True)),
        ("OrderCancelled (by customer)", events.OrderCancelled(order_id=1, user_id=customer, by="customer", stock_restored=True)),
        ("StockLow", events.StockLow(product_id=shop["lip"]["id"], variant_id=pink["id"], stock=1, threshold=3)),
        ("StockOut", events.StockOut(product_id=cream["id"], variant_id=None)),
        ("ProductBackInStock", events.ProductBackInStock(product_id=cream["id"])),
        ("ProductPriceDropped", events.ProductPriceDropped(product_id=cream["id"], old_price_from=Decimal("10.00"),
                                                           new_price_from=Decimal("8.00"), sale_started=False)),
        ("ProductPriceDropped (below threshold)", events.ProductPriceDropped(
            product_id=cream["id"], old_price_from=Decimal("8.00"), new_price_from=Decimal("7.90"), sale_started=False)),
    ]
    by_event = dict(handlers.HANDLERS)
    measured: dict[str, int] = {}
    for name, event in cases:
        before = len(await rows(db_session))
        with count_queries() as counter:
            await by_event[type(event)](event)
        measured[name] = len(counter.pipeline)
        assert len(await rows(db_session)) > before or "below" in name, name
    print("\nHANDLER STATEMENTS", measured)
    assert measured == {
        "OrderPlaced": 2, "OrderStatusChanged": 1, "OrderCancelled (by admin)": 1, "OrderCancelled (by customer)": 2,
        "StockLow": 3, "StockOut": 3, "ProductBackInStock": 4, "ProductPriceDropped": 4,
        "ProductPriceDropped (below threshold)": 1,
    }
    fans = {n.user_id for n in await rows(db_session, Notification.type == "favorite_back_in_stock")}
    assert len(fans) == 3  # one statement for three fans


async def test_dispatch_statement_counts(
    client: AsyncClient, customer_headers: dict, db_session: AsyncSession, count_queries: Any
) -> None:
    customer = await me(client, customer_headers)
    await add_device(client, customer_headers, "phone-1")
    await add_device(client, customer_headers, "phone-2")
    await add_drafts(db_session, [Draft(NotificationType.message, templates.Text("t", "b"), customer)])
    await db_session.commit()
    with count_queries() as counter:
        assert await dispatch_due() == 1
    # claim: select + update, then an empty claim select; deliver: devices + badge + finish.
    print("\nDISPATCH STATEMENTS (one row, two devices)", len(counter.pipeline))
    assert len(counter.pipeline) == 6
