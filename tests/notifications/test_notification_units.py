"""Pure units: texts, quiet hours, link rules, push payloads, FCM error mapping, startup refusal."""

from datetime import datetime, time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.firebase import push
from app.modules.notifications import templates
from app.modules.notifications.dispatcher import Claimed, channel_for, messages_for, payload_data
from app.modules.notifications.enums import NotificationType
from app.modules.notifications.links import Link, check_recipient_link
from app.modules.notifications.outbox import Draft, plan
from app.modules.notifications.quiet_hours import deferred_until, effective_schedule
from app.modules.settings.service import NotificationSettings, notification_settings_from
from app.shared.enums import LinkType

S = NotificationSettings()


def make_settings(**values: Any) -> Settings:
    """A Settings built from these values and the test environment only (never a .env file)."""
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


# --------------------------------------------------------------------------------- texts


def test_customer_order_texts() -> None:
    assert templates.order_placed(12).title == "تم استلام طلبك #12 ✅"
    assert templates.order_preparing(12).title == "طلبك #12 قيد التجهيز"
    assert templates.order_shipped(12).title == "تم شحن طلبك #12 🚚"
    assert templates.order_ready_for_pickup(12).title == "طلبك #12 جاهز للاستلام 🛍️"
    assert templates.order_delivered(12, is_pickup=False).title == "تم توصيل طلبك #12، نتمنى أن تنال المنتجات إعجابك"
    assert templates.order_delivered(12, is_pickup=True).title == "تم استلام طلبك #12، شكرًا لك"
    cancelled = templates.order_cancelled_by_store(12, "المنتج غير متوفر")
    assert (cancelled.title, cancelled.body) == ("أُلغي طلبك #12", "المنتج غير متوفر")


def test_admin_texts() -> None:
    new = templates.admin_new_order(7, 3, "24.00", "استلام من المتجر")
    assert new.title == "طلب جديد #7"
    assert "3" in new.body and "24.00" in new.body and "استلام من المتجر" in new.body
    assert templates.admin_customer_cancelled(7, None).title == "ألغى العميل الطلب #7"
    assert templates.admin_stock_low("Lipstick", "أحمر", 2).title == "Lipstick (أحمر): تبقّى قطعتان"
    assert templates.admin_stock_low("Cream", None, 1).title == "Cream: تبقّى قطعة واحدة"
    assert templates.admin_stock_out("Lipstick", "وردي").title == "نفد مخزون Lipstick (وردي)"


def test_counted_nouns_follow_arabic_number_agreement() -> None:
    assert [templates._products_count(n) for n in (1, 2, 3, 11, 100)] == [
        "منتج واحد", "منتجان", "3 منتجات", "11 منتجًا", "100 منتج"
    ]
    assert [templates._pieces_count(n) for n in (1, 2, 3, 11)] == [
        "قطعة واحدة", "قطعتان", "3 قطع", "11 قطعة"
    ]


def test_cart_reminder_text_mentions_first_product_and_others() -> None:
    assert "Cream" in templates.cart_reminder("Cream", 0).body
    many = templates.cart_reminder("Cream", 2).body
    assert many == "Cream ومنتجان آخران بانتظارك في السلة."


def test_texts_are_clipped_to_column_sizes() -> None:
    text = templates.order_cancelled_by_store(1, "x" * 900)
    assert len(text.body) == templates.BODY_MAX
    long_name = templates.admin_stock_out("n" * 400, None)
    assert len(long_name.title) == templates.TITLE_MAX


# --------------------------------------------------------------------------- quiet hours


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        ("2026-07-01 18:59", None),  # 20:59 Berlin (summer, UTC+2)
        ("2026-07-01 19:00", "2026-07-02 07:00"),  # 21:00 -> 09:00 next day
        ("2026-07-01 23:30", "2026-07-02 07:00"),  # after midnight UTC, still the same night
        ("2026-07-02 06:59", "2026-07-02 07:00"),
        ("2026-07-02 07:00", None),  # 09:00 exactly: allowed
        ("2026-01-15 20:30", "2026-01-16 08:00"),  # winter, UTC+1
        ("2026-10-24 21:00", "2026-10-25 08:00"),  # the night summer time ends
        ("2026-03-28 21:00", "2026-03-29 07:00"),  # the night summer time starts
    ],
)
def test_quiet_hours_deferral(utc: str, expected: str | None) -> None:
    result = deferred_until(datetime.fromisoformat(utc), S)
    assert result == (datetime.fromisoformat(expected) if expected else None)


def test_only_some_types_respect_quiet_hours() -> None:
    night = datetime.fromisoformat("2026-07-01 22:00")
    for type_ in (NotificationType.marketing, NotificationType.favorite_back_in_stock,
                  NotificationType.favorite_price_drop, NotificationType.cart_reminder):
        assert effective_schedule(type_, night, S) == (datetime.fromisoformat("2026-07-02 07:00"), True)
    for type_ in (NotificationType.order_update, NotificationType.admin_alert, NotificationType.message):
        assert effective_schedule(type_, night, S) == (night, False)


def test_quiet_hours_settings_from_store_values() -> None:
    custom = notification_settings_from({"timezone": '"Europe/Vienna"', "quiet_hours_start": '"22:30"',
                                         "quiet_hours_end": '"07:15"', "price_drop_min_percent": "10"})
    assert (custom.timezone, custom.quiet_hours_start, custom.quiet_hours_end) == ("Europe/Vienna", time(22, 30), time(7, 15))
    assert custom.price_drop_min_percent == 10
    broken = notification_settings_from({"timezone": '"Mars/Base"', "quiet_hours_start": '"25:99"', "cart_reminder_after_hours": "0"})
    assert (broken.timezone, broken.quiet_hours_start, broken.cart_reminder_after_hours) == ("Europe/Berlin", time(21, 0), 24)
    # start == end: no quiet hours at all
    assert deferred_until(datetime(2026, 7, 1, 22), NotificationSettings(quiet_hours_start=time(9), quiet_hours_end=time(9))) is None


# ------------------------------------------------------------------------- link safety


def test_admin_links_only_for_admins() -> None:
    for link_type in (LinkType.admin_order, LinkType.admin_product):
        link = Link(link_type, "5")
        check_recipient_link(link, recipient_is_admin=True, order_owner_id=None, recipient_id=1)
        with pytest.raises(ValueError):
            check_recipient_link(link, recipient_is_admin=False, order_owner_id=None, recipient_id=1)


def test_order_links_only_to_the_owner() -> None:
    link = Link(LinkType.order, "9")
    check_recipient_link(link, recipient_is_admin=False, order_owner_id=3, recipient_id=3)
    for owner, recipient in ((3, 4), (None, 4), (3, None)):
        with pytest.raises(ValueError):
            check_recipient_link(link, recipient_is_admin=False, order_owner_id=owner, recipient_id=recipient)


def test_plan_refuses_unsafe_rows() -> None:
    now = datetime(2026, 7, 1, 12)
    text = templates.order_placed(1)
    with pytest.raises(ValueError):  # a broadcast can never carry an admin link
        plan(Draft(NotificationType.admin_alert, text, None, link=Link(LinkType.admin_order, "1")), now, S)
    with pytest.raises(ValueError):  # a customer can never get an admin link
        plan(Draft(NotificationType.admin_alert, text, 5, link=Link(LinkType.admin_order, "1")), now, S, admin_ids={6})
    with pytest.raises(ValueError):  # an order link to someone else's order
        plan(Draft(NotificationType.order_update, text, 5, link=Link(LinkType.order, "1"), order_owner_id=6), now, S)
    ok = plan(Draft(NotificationType.admin_alert, text, 6, link=Link(LinkType.admin_order, "1")), now, S, admin_ids={6})
    assert ok.row["status"] == "queued" and ok.row["scheduled_at"] == now


# ------------------------------------------------------------------------------ payload


def _claimed(type_: NotificationType, link_type: LinkType = LinkType.none, link_value: str | None = None,
             order_id: int | None = None) -> Claimed:
    return Claimed(41, type_, 3, "t", "b", link_type, link_value, order_id)


def test_payload_data_fields() -> None:
    data = payload_data(_claimed(NotificationType.order_update, LinkType.order, "9", 9))
    assert data == {"notification_id": "41", "type": "order_update", "link_type": "order", "link_value": "9", "order_id": "9"}
    assert all(isinstance(v, str) for v in data.values())
    assert payload_data(_claimed(NotificationType.marketing))["link_value"] == ""


def test_customer_payload_never_carries_admin_links() -> None:
    leaked = payload_data(_claimed(NotificationType.order_update, LinkType.admin_order, "9"))
    assert (leaked["link_type"], leaked["link_value"]) == ("none", "")
    admin = payload_data(_claimed(NotificationType.admin_alert, LinkType.admin_order, "9"))
    assert admin["link_type"] == "admin_order"


def test_channels_and_badges() -> None:
    assert channel_for(NotificationType.order_update) == channel_for(NotificationType.admin_alert) == "orders"
    for type_ in (NotificationType.marketing, NotificationType.message, NotificationType.cart_reminder):
        assert channel_for(type_) == "marketing"

    class Device:
        def __init__(self, token: str, user_id: int) -> None:
            self.fcm_token, self.user_id = token, user_id

    messages = messages_for(_claimed(NotificationType.order_update), [Device("a", 3), Device("b", 4)], {3: 2})
    assert [(m.token, m.channel, m.badge) for m in messages] == [("a", "orders", 3), ("b", "orders", 1)]


# ------------------------------------------------------------------------- push backend


def test_fcm_error_mapping() -> None:
    from firebase_admin import exceptions, messaging

    assert push.error_code(messaging.UnregisteredError("gone")) == push.UNREGISTERED
    assert push.error_code(messaging.SenderIdMismatchError("x")) == push.SENDER_ID_MISMATCH
    assert push.error_code(exceptions.InvalidArgumentError("bad")) == push.INVALID_ARGUMENT
    assert push.error_code(exceptions.UnavailableError("later")) == push.UNAVAILABLE
    assert push.error_code(exceptions.InternalError("oops")) == push.INTERNAL
    assert push.error_code(ValueError("?")) == push.OTHER
    assert push.PushResult("t", push.UNREGISTERED).invalid_token
    assert push.PushResult("t", push.INTERNAL).retryable
    assert not push.PushResult("t", push.OTHER).retryable and not push.PushResult("t", push.OTHER).invalid_token


@pytest.mark.anyio
async def test_log_backend_reports_success() -> None:
    results = await push.LogPushSender().send([push.PushMessage("tok-1", "t", "b")])
    assert [r.code for r in results] == [push.OK]


def test_firebase_backend_needs_credentials(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="FIREBASE_CREDENTIALS_PATH"):
        make_settings(PUSH_BACKEND="firebase")
    with pytest.raises(ValidationError, match="FIREBASE_CREDENTIALS_PATH"):
        make_settings(PUSH_BACKEND="firebase", FIREBASE_CREDENTIALS_PATH=tmp_path / "missing.json")
    with pytest.raises(ValidationError, match="PUSH_BACKEND"):
        make_settings(PUSH_BACKEND="carrier-pigeon")
    credentials = tmp_path / "service-account.json"
    credentials.write_text("{}", encoding="utf-8")
    assert make_settings(PUSH_BACKEND="firebase", FIREBASE_CREDENTIALS_PATH=credentials).PUSH_BACKEND == "firebase"


def test_worker_in_app_defaults_to_development_only() -> None:
    assert make_settings(APP_ENV="development", RUN_WORKER_IN_APP=None).run_worker_in_app is True
    assert make_settings(APP_ENV="production", RUN_WORKER_IN_APP=None).run_worker_in_app is False
    assert make_settings(APP_ENV="production", RUN_WORKER_IN_APP=True).run_worker_in_app is True


def test_only_the_token_deprecation_is_silenced() -> None:
    """fcm_message() hides exactly firebase-admin's Message.token warning, nothing else, and leaves
    the global warning filters as they were."""
    import warnings

    filters_before = list(warnings.filters)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        message = push.fcm_message(push.PushMessage("tok", "t", "b", {"k": "v"}, "orders", 2))
        warnings.warn("some other deprecation", DeprecationWarning, stacklevel=1)
    assert [str(w.message) for w in caught] == ["some other deprecation"]
    assert message.token == "tok" and message.fid is None
    assert message.android.notification.channel_id == "orders" and message.apns.payload.aps.badge == 2
    assert list(warnings.filters) == filters_before

    from firebase_admin import messaging

    with pytest.warns(DeprecationWarning, match="Message.token is deprecated"):  # still raised elsewhere
        messaging.Message(token="tok")
