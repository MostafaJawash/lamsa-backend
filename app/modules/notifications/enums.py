from enum import StrEnum


class NotificationTarget(StrEnum):
    all = "all"
    user = "user"


class NotificationType(StrEnum):
    order_update = "order_update"
    admin_alert = "admin_alert"
    marketing = "marketing"
    message = "message"
    favorite_back_in_stock = "favorite_back_in_stock"
    favorite_price_drop = "favorite_price_drop"
    cart_reminder = "cart_reminder"


class NotificationStatus(StrEnum):
    queued = "queued"
    scheduled = "scheduled"
    sending = "sending"
    sent = "sent"
    cancelled = "cancelled"


# Types that respect quiet hours and user preferences.
QUIET_HOURS_TYPES = frozenset(
    {NotificationType.marketing, NotificationType.favorite_back_in_stock, NotificationType.favorite_price_drop,
     NotificationType.cart_reminder}
)
ALWAYS_ON_TYPES = frozenset({NotificationType.order_update, NotificationType.admin_alert, NotificationType.message})
