from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.modules.notifications.enums import NotificationStatus, NotificationTarget, NotificationType
from app.shared.dependencies.pagination import Page
from app.shared.enums import LinkType


def to_naive_utc(value: datetime | None) -> datetime | None:
    """Aware datetimes are converted to UTC; naive ones are taken as UTC."""
    if value is not None and value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.replace(microsecond=0) if value is not None else None


UtcDateTime = Annotated[datetime, AfterValidator(to_naive_utc)]


class LinkOut(BaseModel):
    type: LinkType
    value: str | None = None


class LinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: LinkType = LinkType.none
    # Loose cap only: the resolver applies the real rules (url <= 255 chars) as INVALID_LINK.
    value: str | None = Field(default=None, max_length=2048)


# ------------------------------------------------------------------------------- inbox


class NotificationOut(BaseModel):
    id: int
    type: NotificationType
    title: str
    body: str
    link: LinkOut
    order_id: int | None
    sent_at: datetime
    is_read: bool
    read_at: datetime | None


class InboxPage(Page[NotificationOut]):
    unread_count: int


class UnreadCountOut(BaseModel):
    unread_count: int


class PreferencesOut(BaseModel):
    order_updates: bool = Field(default=True, description="Always on: order updates cannot be switched off")
    notify_marketing: bool
    notify_favorites: bool
    notify_cart_reminders: bool


class PreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notify_marketing: bool | None = None
    notify_favorites: bool | None = None
    notify_cart_reminders: bool | None = None


# ------------------------------------------------------------------------------- admin


class AdminNotificationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audience: NotificationTarget = Field(description="all: marketing to everyone; user: a message to one user")
    user_id: int | None = None
    title: str = Field(min_length=1, max_length=150)
    body: str = Field(min_length=1, max_length=500)
    link: LinkIn | None = None
    scheduled_at: UtcDateTime | None = Field(
        default=None, description="UTC (or with an offset); 1 minute to 90 days ahead. Empty: send now"
    )

    @model_validator(mode="after")
    def _audience_matches_user(self) -> "AdminNotificationCreate":
        if self.audience == NotificationTarget.user and self.user_id is None:
            raise ValueError("user_id is required when audience is 'user'")
        if self.audience == NotificationTarget.all and self.user_id is not None:
            raise ValueError("user_id must be empty when audience is 'all'")
        return self


class AdminNotificationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=150)
    body: str | None = Field(default=None, min_length=1, max_length=500)
    link: LinkIn | None = None
    scheduled_at: UtcDateTime | None = None


class AdminNotificationOut(BaseModel):
    id: int
    type: NotificationType
    audience: NotificationTarget
    user_id: int | None
    user_name: str | None
    title: str
    body: str
    link: LinkOut
    status: NotificationStatus
    scheduled_at: datetime | None
    sent_at: datetime | None
    created_at: datetime
    created_by_user_id: int | None
    created_by_name: str | None
    recipients_count: int
    success_count: int
    failure_count: int
    deferred_by_quiet_hours: bool


class PushTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="إشعار تجريبي", min_length=1, max_length=150)
    body: str = Field(default="إذا وصلك هالإشعار، الإعدادات تمام ✅", min_length=1, max_length=500)


class PushTestResultOut(BaseModel):
    token: str = Field(description="The first characters of the FCM token")
    ok: bool
    code: str
    detail: str | None


class PushTestOut(BaseModel):
    devices: int
    results: list[PushTestResultOut]
