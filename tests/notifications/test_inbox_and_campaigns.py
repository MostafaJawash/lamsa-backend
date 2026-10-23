"""Inbox, preferences and the admin campaign endpoints."""

from datetime import datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications import templates
from app.modules.notifications.dispatcher import dispatch_due
from app.modules.notifications.enums import NotificationType
from app.modules.notifications.links import Link
from app.modules.notifications.outbox import Draft, add_drafts
from app.shared.enums import LinkType
from catalog_helpers import ADMIN, code_of, create_category, ok, published_product
from conftest import FakeClock, FakePushSender, register
from notification_helpers import ADMIN_NOTIFICATIONS, NOTIFICATIONS, PREFERENCES, add_device, demote, me, rows

pytestmark = pytest.mark.anyio

NOON = datetime(2026, 7, 1, 10, 0)  # 12:00 in Berlin
NIGHT = datetime(2026, 7, 1, 20, 0)  # 22:00 in Berlin


async def _send(session: AsyncSession, *drafts: Draft, admin_ids: tuple[int, ...] = ()) -> None:
    await add_drafts(session, list(drafts), admin_ids=admin_ids)
    await session.commit()
    await dispatch_due()


def _text(title: str) -> templates.Text:
    return templates.Text(title, "body")


async def _inbox(client: AsyncClient, headers: dict[str, str], **params: Any) -> dict[str, Any]:
    data: dict[str, Any] = ok(await client.get(NOTIFICATIONS, params=params, headers=headers))
    return data


# ------------------------------------------------------------------------------- inbox


async def test_inbox_lists_own_and_broadcast_rows_newest_first(
    client: AsyncClient, customer_headers: dict, db_session: AsyncSession, frozen_clock: FakeClock
) -> None:
    frozen_clock.now = NOON
    me_id = await me(client, customer_headers)
    other = (await register(client, 50, name="Other"))["user"]["id"]
    await _send(db_session, Draft(NotificationType.message, _text("mine"), me_id))
    frozen_clock.advance(minutes=1)
    await _send(db_session, Draft(NotificationType.message, _text("theirs"), other))
    frozen_clock.advance(minutes=1)
    await _send(db_session, Draft(NotificationType.marketing, _text("everyone"), None))
    await add_drafts(db_session, [Draft(NotificationType.message, _text("not sent yet"), me_id)])
    await db_session.commit()

    inbox = await _inbox(client, customer_headers)
    assert [(n["title"], n["is_read"]) for n in inbox["items"]] == [("everyone", False), ("mine", False)]
    assert (inbox["total"], inbox["unread_count"]) == (2, 2)
    assert inbox["items"][0]["link"] == {"type": "none", "value": None}
    assert ok(await client.get(f"{NOTIFICATIONS}/unread-count", headers=customer_headers)) == {"unread_count": 2}

    mine = inbox["items"][1]["id"]
    assert ok(await client.post(f"{NOTIFICATIONS}/{mine}/read", headers=customer_headers)) == {"unread_count": 1}
    assert ok(await client.post(f"{NOTIFICATIONS}/{mine}/read", headers=customer_headers)) == {"unread_count": 1}  # idempotent
    read = (await _inbox(client, customer_headers))["items"][1]
    assert read["is_read"] is True and read["read_at"] == frozen_clock.now.isoformat()

    theirs = (await rows(db_session))[1].id
    assert code_of(await client.post(f"{NOTIFICATIONS}/{theirs}/read", headers=customer_headers)) == "NOTIFICATION_NOT_FOUND"
    assert code_of(await client.post(f"{NOTIFICATIONS}/999999/read", headers=customer_headers)) == "NOTIFICATION_NOT_FOUND"

    assert ok(await client.post(f"{NOTIFICATIONS}/read-all", headers=customer_headers)) == {"unread_count": 0}
    assert (await _inbox(client, customer_headers))["unread_count"] == 0
    assert (await client.get(NOTIFICATIONS)).status_code == 401


async def test_marketing_switched_off_is_hidden_from_the_inbox(
    client: AsyncClient, customer_headers: dict, db_session: AsyncSession, frozen_clock: FakeClock
) -> None:
    frozen_clock.now = NOON  # marketing waits during quiet hours: never depend on the time of day
    me_id = await me(client, customer_headers)
    await _send(db_session, Draft(NotificationType.marketing, _text("sale"), None),
                Draft(NotificationType.message, _text("hello"), me_id))
    ok(await client.patch(PREFERENCES, json={"notify_marketing": False}, headers=customer_headers))
    assert [n["title"] for n in (await _inbox(client, customer_headers))["items"]] == ["hello"]
    assert ok(await client.get(f"{NOTIFICATIONS}/unread-count", headers=customer_headers)) == {"unread_count": 1}
    ok(await client.patch(PREFERENCES, json={"notify_marketing": True}, headers=customer_headers))
    assert len((await _inbox(client, customer_headers))["items"]) == 2


async def test_links_to_hidden_targets_and_admin_links_are_dropped(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, db_session: AsyncSession, frozen_clock: FakeClock
) -> None:
    frozen_clock.now = NOON  # marketing waits during quiet hours: never depend on the time of day
    category = await create_category(client, admin_headers, "Inbox Cat", slug="inbox-cat")
    product = await published_product(client, admin_headers, name="Inbox Cream", category_id=category["id"], price="5.00", stock=3)
    admin_id = await me(client, admin_headers)
    await _send(db_session, Draft(NotificationType.marketing, _text("product"), None, link=Link(LinkType.product, product["slug"])),
                Draft(NotificationType.admin_alert, _text("alert"), admin_id, link=Link(LinkType.admin_order, "5")),
                admin_ids=(admin_id,))
    assert (await _inbox(client, customer_headers))["items"][0]["link"] == {"type": "product", "value": product["slug"]}
    ok(await client.patch(f"{ADMIN}/products/{product['id']}", json={"is_active": False}, headers=admin_headers))
    assert (await _inbox(client, customer_headers))["items"][0]["link"] == {"type": "none", "value": None}

    admin_inbox = await _inbox(client, admin_headers)
    assert admin_inbox["items"][0]["link"] == {"type": "admin_order", "value": "5"}
    await demote(db_session, admin_id)
    assert all(n["link"]["type"] != "admin_order" for n in (await _inbox(client, admin_headers))["items"])


async def test_preferences(client: AsyncClient, customer_headers: dict) -> None:
    assert ok(await client.get(PREFERENCES, headers=customer_headers)) == {
        "order_updates": True, "notify_marketing": True, "notify_favorites": True, "notify_cart_reminders": True}
    changed = ok(await client.patch(PREFERENCES, json={"notify_favorites": False, "notify_cart_reminders": False}, headers=customer_headers))
    assert (changed["notify_marketing"], changed["notify_favorites"], changed["notify_cart_reminders"]) == (True, False, False)
    assert ok(await client.get(PREFERENCES, headers=customer_headers)) == changed
    assert (await client.patch(PREFERENCES, json={"order_updates": False}, headers=customer_headers)).status_code == 422


# ------------------------------------------------------------------------- admin campaigns


async def test_create_campaigns_now_scheduled_and_deferred(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, frozen_clock: FakeClock, pushes: FakePushSender
) -> None:
    frozen_clock.now = NOON
    customer = await me(client, customer_headers)
    await add_device(client, customer_headers, "phone")
    now = ok(await client.post(ADMIN_NOTIFICATIONS, json={"audience": "all", "title": "Sale", "body": "Now"}, headers=admin_headers), 201)
    assert (now["type"], now["status"], now["audience"], now["deferred_by_quiet_hours"]) == ("marketing", "queued", "all", False)
    assert now["created_by_name"] == "Catalog Admin"

    later = NOON + timedelta(days=2)
    scheduled = ok(await client.post(ADMIN_NOTIFICATIONS, json={
        "audience": "user", "user_id": customer, "title": "Hi", "body": "For you", "scheduled_at": later.isoformat() + "Z",
        "link": {"type": "url", "value": "https://lamsa.example/offer"},
    }, headers=admin_headers), 201)
    assert (scheduled["type"], scheduled["status"], scheduled["scheduled_at"]) == ("message", "scheduled", later.isoformat())
    assert scheduled["user_name"] == "Customer" and scheduled["link"] == {"type": "url", "value": "https://lamsa.example/offer"}

    at_night = ok(await client.post(ADMIN_NOTIFICATIONS, json={
        "audience": "all", "title": "Night", "body": "Sale", "scheduled_at": "2026-07-02T23:30:00+02:00"}, headers=admin_headers), 201)
    assert (at_night["status"], at_night["scheduled_at"], at_night["deferred_by_quiet_hours"]) == (
        "scheduled", "2026-07-03T07:00:00", True)
    # A message to one user ignores quiet hours.
    frozen_clock.now = NIGHT
    message = ok(await client.post(ADMIN_NOTIFICATIONS, json={"audience": "user", "user_id": customer, "title": "x", "body": "y"},
                                   headers=admin_headers), 201)
    assert (message["status"], message["deferred_by_quiet_hours"]) == ("queued", False)
    night_sale = ok(await client.post(ADMIN_NOTIFICATIONS, json={"audience": "all", "title": "x", "body": "y"}, headers=admin_headers), 201)
    assert (night_sale["status"], night_sale["deferred_by_quiet_hours"]) == ("scheduled", True)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"audience": "all", "scheduled_at": "2026-07-01T10:00:30"}, "INVALID_SCHEDULE"),  # under a minute ahead
        ({"audience": "all", "scheduled_at": "2026-07-01T09:00:00"}, "INVALID_SCHEDULE"),  # the past
        ({"audience": "all", "scheduled_at": "2026-09-30T10:00:01"}, "INVALID_SCHEDULE"),  # over 90 days
        ({"audience": "all", "link": {"type": "admin_order", "value": "1"}}, "INVALID_LINK"),
        ({"audience": "all", "link": {"type": "order", "value": "1"}}, "INVALID_LINK"),
        ({"audience": "all", "link": {"type": "url", "value": "http://plain.example"}}, "INVALID_LINK"),
        ({"audience": "all", "link": {"type": "url", "value": "https://x.example/" + "a" * 250}}, "INVALID_LINK"),
        ({"audience": "all", "link": {"type": "product", "value": "no-such-product"}}, "INVALID_LINK"),
        ({"audience": "all", "link": {"type": "product"}}, "INVALID_LINK"),
        ({"audience": "all", "link": {"type": "sale", "value": "x"}}, "INVALID_LINK"),
        ({"audience": "user", "user_id": 999999}, "USER_NOT_FOUND"),
        ({"audience": "user"}, "VALIDATION_ERROR"),
        ({"audience": "all", "user_id": 1}, "VALIDATION_ERROR"),
    ],
)
async def test_create_rejections(client: AsyncClient, admin_headers: dict, frozen_clock: FakeClock, body: dict, code: str) -> None:
    frozen_clock.now = NOON
    assert code_of(await client.post(ADMIN_NOTIFICATIONS, json={"title": "t", "body": "b", **body}, headers=admin_headers)) == code


async def test_edit_cancel_and_send_now_only_while_scheduled(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, frozen_clock: FakeClock, pushes: FakePushSender
) -> None:
    frozen_clock.now = NOON
    await add_device(client, customer_headers, "phone")
    create = {"audience": "all", "title": "Later", "body": "b", "scheduled_at": (NOON + timedelta(days=1)).isoformat()}
    one = ok(await client.post(ADMIN_NOTIFICATIONS, json=create, headers=admin_headers), 201)
    url = f"{ADMIN_NOTIFICATIONS}/{one['id']}"

    edited = ok(await client.patch(url, json={"title": "Later!", "link": {"type": "new"},
                                              "scheduled_at": "2026-07-02T21:30:00"}, headers=admin_headers))
    assert (edited["title"], edited["link"]["type"]) == ("Later!", "new")
    assert (edited["scheduled_at"], edited["deferred_by_quiet_hours"]) == ("2026-07-03T07:00:00", True)
    assert code_of(await client.patch(url, json={"scheduled_at": "2026-07-01T10:00:10"}, headers=admin_headers)) == "INVALID_SCHEDULE"

    sent = ok(await client.post(f"{url}/send-now", headers=admin_headers))
    assert (sent["status"], sent["scheduled_at"], sent["deferred_by_quiet_hours"]) == ("queued", NOON.isoformat(), False)
    for action in ("send-now", "cancel"):
        response = await client.post(f"{url}/{action}", headers=admin_headers)
        assert (response.status_code, code_of(response)) == (409, "NOTIFICATION_NOT_EDITABLE")
    assert code_of(await client.patch(url, json={"title": "x"}, headers=admin_headers)) == "NOTIFICATION_NOT_EDITABLE"

    await dispatch_due()
    detail = ok(await client.get(url, headers=admin_headers))
    assert (detail["status"], detail["recipients_count"], detail["success_count"]) == ("sent", 1, 1)

    two = ok(await client.post(ADMIN_NOTIFICATIONS, json=create, headers=admin_headers), 201)
    assert ok(await client.post(f"{ADMIN_NOTIFICATIONS}/{two['id']}/cancel", headers=admin_headers))["status"] == "cancelled"
    frozen_clock.advance(days=2)
    await dispatch_due()
    assert len(pushes.sent) == 1  # the cancelled one never went out
    assert code_of(await client.get(f"{ADMIN_NOTIFICATIONS}/999999", headers=admin_headers)) == "NOTIFICATION_NOT_FOUND"


async def test_send_now_ignores_quiet_hours(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, frozen_clock: FakeClock, pushes: FakePushSender
) -> None:
    frozen_clock.now = NIGHT
    await add_device(client, customer_headers, "phone")
    deferred = ok(await client.post(ADMIN_NOTIFICATIONS, json={"audience": "all", "title": "x", "body": "y"}, headers=admin_headers), 201)
    assert deferred["status"] == "scheduled"
    await dispatch_due()
    assert pushes.sent == []
    ok(await client.post(f"{ADMIN_NOTIFICATIONS}/{deferred['id']}/send-now", headers=admin_headers))
    await dispatch_due()
    assert [m.token for m in pushes.sent] == ["phone"]


async def test_admin_list_filters_and_access(
    client: AsyncClient, admin_headers: dict, customer_headers: dict, db_session: AsyncSession, frozen_clock: FakeClock
) -> None:
    frozen_clock.now = NOON
    customer = await me(client, customer_headers)
    ok(await client.post(ADMIN_NOTIFICATIONS, json={"audience": "all", "title": "a", "body": "b"}, headers=admin_headers), 201)
    ok(await client.post(ADMIN_NOTIFICATIONS, json={"audience": "user", "user_id": customer, "title": "c", "body": "d",
                                                    "scheduled_at": "2026-07-05T10:00:00"}, headers=admin_headers), 201)
    await add_drafts(db_session, [Draft(NotificationType.order_update, _text("order"), customer)])
    await db_session.commit()

    listed = ok(await client.get(ADMIN_NOTIFICATIONS, headers=admin_headers))
    assert [n["title"] for n in listed["items"]] == ["c", "a"] and listed["total"] == 2  # campaigns only by default
    assert [n["title"] for n in ok(await client.get(ADMIN_NOTIFICATIONS, params={"status": "scheduled"}, headers=admin_headers))["items"]] == ["c"]
    assert [n["title"] for n in ok(await client.get(ADMIN_NOTIFICATIONS, params={"audience": "all"}, headers=admin_headers))["items"]] == ["a"]
    assert [n["title"] for n in ok(await client.get(ADMIN_NOTIFICATIONS, params={"type": "order_update"}, headers=admin_headers))["items"]] == ["order"]
    for method, path in (("get", ADMIN_NOTIFICATIONS), ("post", ADMIN_NOTIFICATIONS)):
        assert (await client.request(method, path, json={}, headers=customer_headers)).status_code == 403
