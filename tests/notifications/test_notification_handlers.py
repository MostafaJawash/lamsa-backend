"""Domain events -> notification rows (order updates, admin alerts, favorites alerts)."""

from datetime import datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.models import Notification, UserAlertLog
from cart_fixtures import FAVORITES, build_shop
from catalog_helpers import ADMIN, ok
from conftest import FakeClock, bearer, make_admin, register
from notification_helpers import demote, me, rows, set_user
from order_helpers import ADMIN_ORDERS, ORDERS, order_from_cart

pytestmark = pytest.mark.anyio


@pytest.fixture
async def shop(client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession) -> dict[str, Any]:
    return await build_shop(client, admin_headers, db_session)


@pytest.fixture
async def admins(client: AsyncClient, admin_headers: dict[str, str], db_session: AsyncSession) -> dict[str, int]:
    """The catalog admin, a second active admin, a deactivated admin and a demoted one."""
    ids = {"main": await me(client, admin_headers)}
    for key, n in (("second", 61), ("inactive", 62), ("demoted", 63)):
        ids[key] = (await register(client, n, name=f"Admin {key}"))["user"]["id"]
        await make_admin(db_session, ids[key])
    await set_user(db_session, ids["inactive"], is_active=False)
    await demote(db_session, ids["demoted"])
    return ids


def _by_user(items: list[Notification]) -> dict[int | None, list[Notification]]:
    grouped: dict[int | None, list[Notification]] = {}
    for n in items:
        grouped.setdefault(n.user_id, []).append(n)
    return grouped


# -------------------------------------------------------------------------------- orders


async def test_order_placed_notifies_customer_and_active_admins_only(
    client: AsyncClient, customer_headers: dict, shop: dict, admins: dict, db_session: AsyncSession
) -> None:
    customer = await me(client, customer_headers)
    order = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 3)], method="pickup")

    placed = await rows(db_session, Notification.type == "order_update")
    assert [(n.user_id, n.title, n.link_type, n.link_value, n.status) for n in placed] == [
        (customer, f"تم استلام طلبك #{order['id']} ✅", "order", str(order["id"]), "queued")
    ]
    assert placed[0].data == {"order_id": order["id"]} and placed[0].target == "user"

    alerts = _by_user(await rows(db_session, Notification.type == "admin_alert"))
    assert set(alerts) == {admins["main"], admins["second"]}  # not the inactive, not the demoted admin
    for user_alerts in alerts.values():
        new_order, low = user_alerts
        assert new_order.title == f"طلب جديد #{order['id']}"
        assert "3" in new_order.body and "24.00" in new_order.body
        assert (new_order.link_type, new_order.link_value) == ("admin_order", str(order["id"]))
        assert low.title == "Cream: تبقّى قطعتان"  # 5 -> 2 crosses the threshold 3
        assert (low.link_type, low.link_value) == ("admin_product", str(shop["simple"]["id"]))


async def test_status_texts_for_delivery(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict, db_session: AsyncSession
) -> None:
    async def walk(order_id: int, steps: list[str]) -> list[str]:
        for step in steps:
            ok(await client.post(f"{ADMIN_ORDERS}/{order_id}/status", json={"to_status": step}, headers=admin_headers))
        return [n.title for n in await rows(db_session, Notification.type == "order_update",
                                            Notification.data["order_id"] == order_id)]

    delivery = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 1)])
    assert await walk(delivery["id"], ["preparing", "shipped", "delivered"]) == [
        f"تم استلام طلبك #{delivery['id']} ✅", f"طلبك #{delivery['id']} قيد التجهيز", f"تم شحن طلبك #{delivery['id']} 🚚",
        f"تم توصيل طلبك #{delivery['id']}، نتمنى أن تنال المنتجات إعجابك",
    ]


async def test_cancellations(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict, admins: dict, db_session: AsyncSession
) -> None:
    by_store = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 1)], method="pickup")
    ok(await client.post(f"{ADMIN_ORDERS}/{by_store['id']}/cancel", json={"reason": "المنتج تالف"}, headers=admin_headers))
    cancelled = (await rows(db_session, Notification.type == "order_update"))[-1]
    assert (cancelled.title, cancelled.body) == (f"أُلغي طلبك #{by_store['id']}", "المنتج تالف")

    by_customer = await order_from_cart(client, customer_headers, [(shop["simple"]["id"], None, 1)], method="pickup")
    before = len(await rows(db_session, Notification.type == "order_update"))
    ok(await client.post(f"{ORDERS}/{by_customer['id']}/cancel", json={"reason": "غيرت رأيي"}, headers=customer_headers))
    assert len(await rows(db_session, Notification.type == "order_update")) == before  # nothing for the customer
    alerts = [n for n in await rows(db_session, Notification.type == "admin_alert") if n.title.startswith("ألغى العميل")]
    assert {n.user_id for n in alerts} == {admins["main"], admins["second"]}
    assert all((n.title, n.body) == (f"ألغى العميل الطلب #{by_customer['id']}", "غيرت رأيي") for n in alerts)


async def test_stock_out_alert_with_variant_label(
    client: AsyncClient, admin_headers: dict, shop: dict, admins: dict, db_session: AsyncSession
) -> None:
    lip, pink = shop["lip"], shop["pink"]
    ok(await client.post(f"{ADMIN}/products/{lip['id']}/variants/{pink['id']}/stock-adjust", json={"delta": -2}, headers=admin_headers))
    out = await rows(db_session, Notification.type == "admin_alert")
    assert {n.user_id for n in out} == {admins["main"], admins["second"]}
    assert {n.title for n in out} == {"نفد مخزون Lipstick (وردي)"}
    # A newly demoted admin gets nothing new.
    await demote(db_session, admins["second"])
    ok(await client.post(f"{ADMIN}/products/{shop['simple']['id']}/stock-adjust", json={"delta": -5}, headers=admin_headers))
    newest = (await rows(db_session, Notification.type == "admin_alert"))[len(out):]
    assert [(n.user_id, n.title) for n in newest] == [(admins["main"], "نفد مخزون Cream")]


# ----------------------------------------------------------------------------- favorites


async def _fan(client: AsyncClient, n: int, *product_ids: int) -> tuple[dict[str, str], int]:
    data = await register(client, n, name=f"Fan {n}")
    headers = bearer(data["access_token"])
    for product_id in product_ids:
        assert (await client.put(f"{FAVORITES}/{product_id}", headers=headers)).status_code == 204
    return headers, data["user"]["id"]


async def test_back_in_stock_alerts_fans_with_cooldown(
    client: AsyncClient, admin_headers: dict, shop: dict, db_session: AsyncSession
) -> None:
    cream = shop["simple"]["id"]
    _, fan = await _fan(client, 70, cream)
    fan_off_headers, fan_off = await _fan(client, 71, cream)
    ok(await client.patch("/api/v1/users/me/notification-preferences", json={"notify_favorites": False}, headers=fan_off_headers))

    adjust = f"{ADMIN}/products/{cream}/stock-adjust"
    ok(await client.post(adjust, json={"delta": -5}, headers=admin_headers))
    ok(await client.post(adjust, json={"delta": 3}, headers=admin_headers))
    alerts = await rows(db_session, Notification.type == "favorite_back_in_stock")
    assert [(n.user_id, n.link_type, n.link_value) for n in alerts] == [(fan, "product", shop["simple"]["slug"])]
    assert "Cream" in alerts[0].body
    log = (await db_session.execute(select(UserAlertLog.user_id, UserAlertLog.kind, UserAlertLog.ref_id))).all()
    assert [tuple(r) for r in log] == [(fan, "favorite_back_in_stock", cream)]

    # Out and back again within the cooldown: no second alert.
    ok(await client.post(adjust, json={"delta": -3}, headers=admin_headers))
    ok(await client.post(adjust, json={"delta": 2}, headers=admin_headers))
    assert len(await rows(db_session, Notification.type == "favorite_back_in_stock")) == 1
    assert fan_off not in {n.user_id for n in alerts}


async def test_back_in_stock_after_the_cooldown(
    client: AsyncClient, admin_headers: dict, shop: dict, db_session: AsyncSession, frozen_clock: FakeClock
) -> None:
    frozen_clock.now = datetime(2026, 7, 1, 9, 0)  # 12:00 Damascus, outside quiet hours
    cream = shop["simple"]["id"]
    _, fan = await _fan(client, 72, cream)
    adjust = f"{ADMIN}/products/{cream}/stock-adjust"
    for delta in (-5, 1):
        ok(await client.post(adjust, json={"delta": delta}, headers=admin_headers))
    frozen_clock.advance(days=7, minutes=1)
    for delta in (-1, 1):
        ok(await client.post(adjust, json={"delta": delta}, headers=admin_headers))
    assert [n.user_id for n in await rows(db_session, Notification.type == "favorite_back_in_stock")] == [fan, fan]


async def test_price_drop_threshold_and_sale_start(
    client: AsyncClient, admin_headers: dict, shop: dict, db_session: AsyncSession
) -> None:
    cream, perfume = shop["simple"], shop["perfume"]
    _, fan = await _fan(client, 73, cream["id"], perfume["id"])

    # Cream 8.00 -> 7.95: below 5 %, nothing.
    ok(await client.patch(f"{ADMIN}/products/{cream['id']}", json={"sale_price": "7.95"}, headers=admin_headers))
    assert await rows(db_session, Notification.type == "favorite_price_drop") == []
    # 7.95 -> 7.00: about 12 %, an alert with the new price.
    ok(await client.patch(f"{ADMIN}/products/{cream['id']}", json={"sale_price": "7.00"}, headers=admin_headers))
    drops = await rows(db_session, Notification.type == "favorite_price_drop")
    assert [(n.user_id, n.link_value) for n in drops] == [(fan, cream["slug"])] and "7.00" in drops[0].body
    # Perfume starts a sale (20.00 -> 19.50 is only 2.5 %, but a sale start always counts).
    p50 = shop["p50"]
    ok(await client.patch(f"{ADMIN}/products/{perfume['id']}/variants/{p50['id']}", json={"sale_price": "19.50"}, headers=admin_headers))
    drops = await rows(db_session, Notification.type == "favorite_price_drop")
    assert [n.link_value for n in drops] == [cream["slug"], perfume["slug"]]
    # Within the cooldown the same product does not alert again.
    ok(await client.patch(f"{ADMIN}/products/{cream['id']}", json={"sale_price": "5.00"}, headers=admin_headers))
    assert len(await rows(db_session, Notification.type == "favorite_price_drop")) == 2


async def test_favorite_alerts_wait_for_quiet_hours_but_orders_do_not(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, shop: dict, db_session: AsyncSession,
    frozen_clock: FakeClock,
) -> None:
    frozen_clock.now = datetime(2026, 7, 1, 20, 0)  # 23:00 in Damascus
    cream = shop["simple"]["id"]
    await _fan(client, 74, cream)
    for delta in (-5, 2):
        ok(await client.post(f"{ADMIN}/products/{cream}/stock-adjust", json={"delta": delta}, headers=admin_headers))
    (alert,) = await rows(db_session, Notification.type == "favorite_back_in_stock")
    assert (alert.status, alert.scheduled_at) == ("scheduled", datetime(2026, 7, 2, 6, 0))  # 09:00 Damascus
    assert alert.data == {"deferred_by_quiet_hours": True}

    await order_from_cart(client, customer_headers, [(cream, None, 1)], method="pickup")
    order_rows = await rows(db_session, Notification.type.in_(["order_update", "admin_alert"]))
    assert order_rows and all(n.status == "queued" and n.scheduled_at == frozen_clock.now for n in order_rows)
