from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.modules.notifications.enums import NotificationStatus, NotificationTarget, NotificationType
from app.modules.notifications.schemas import (
    AdminNotificationCreate,
    AdminNotificationOut,
    AdminNotificationUpdate,
    InboxPage,
    PreferencesOut,
    PreferencesUpdate,
    PushTestIn,
    PushTestOut,
    UnreadCountOut,
)
from app.modules.notifications.service import AdminNotificationService, InboxService, PreferencesService
from app.shared.dependencies.auth import AdminUser, CurrentUser, DbSession
from app.shared.dependencies.pagination import Page, PageParamsDep

router = APIRouter(prefix="/notifications", tags=["notifications"])
preferences_router = APIRouter(prefix="/users/me/notification-preferences", tags=["notifications"])
admin_router = APIRouter(prefix="/admin/notifications", tags=["admin-notifications"])


def get_inbox(session: DbSession, user: CurrentUser) -> InboxService:
    return InboxService(session, user)


def get_preferences(session: DbSession, user: CurrentUser) -> PreferencesService:
    return PreferencesService(session, user)


def get_admin_service(session: DbSession, admin: AdminUser) -> AdminNotificationService:
    return AdminNotificationService(session, admin)


Inbox = Annotated[InboxService, Depends(get_inbox)]
Preferences = Annotated[PreferencesService, Depends(get_preferences)]
Admin = Annotated[AdminNotificationService, Depends(get_admin_service)]

# ------------------------------------------------------------------------------- inbox


@router.get("", response_model=InboxPage)
async def list_notifications(inbox: Inbox, params: PageParamsDep) -> InboxPage:
    """The user's inbox, newest first: their own notifications and broadcasts (marketing is
    hidden while notify_marketing is off)."""
    return await inbox.list(params)


@router.get("/unread-count", response_model=UnreadCountOut)
async def unread_count(inbox: Inbox) -> UnreadCountOut:
    return await inbox.unread_count()


@router.post("/read-all", response_model=UnreadCountOut)
async def mark_all_read(inbox: Inbox) -> UnreadCountOut:
    return await inbox.mark_all_read()


@router.post("/{notification_id}/read", response_model=UnreadCountOut)
async def mark_read(notification_id: int, inbox: Inbox) -> UnreadCountOut:
    """Idempotent. 404 NOTIFICATION_NOT_FOUND when it is not in the user's inbox."""
    return await inbox.mark_read(notification_id)


@preferences_router.get("", response_model=PreferencesOut)
async def get_preferences_route(preferences: Preferences) -> PreferencesOut:
    return preferences.get()


@preferences_router.patch("", response_model=PreferencesOut)
async def update_preferences(data: PreferencesUpdate, preferences: Preferences) -> PreferencesOut:
    return await preferences.update(data)


# ------------------------------------------------------------------------------- admin


@admin_router.post("", response_model=AdminNotificationOut, status_code=status.HTTP_201_CREATED)
async def create_notification(data: AdminNotificationCreate, service: Admin) -> AdminNotificationOut:
    """audience 'all' sends marketing to everyone (quiet hours apply: see deferred_by_quiet_hours),
    'user' sends a message to one user. Catalog links only."""
    return await service.create(data)


@admin_router.get("", response_model=Page[AdminNotificationOut])
async def list_notifications_admin(
    service: Admin,
    params: PageParamsDep,
    status_filter: Annotated[NotificationStatus | None, Query(alias="status")] = None,
    type_filter: Annotated[NotificationType | None, Query(alias="type", description="Default: campaigns (marketing, message)")] = None,
    audience: NotificationTarget | None = None,
) -> Page[AdminNotificationOut]:
    return await service.list(params, status_filter, type_filter, audience)


@admin_router.post("/test", response_model=PushTestOut)
async def test_push(service: Admin, data: PushTestIn | None = None) -> PushTestOut:
    """Push to YOUR OWN devices only (no inbox row) and show the result per device."""
    return await service.test(data or PushTestIn())


@admin_router.get("/{notification_id}", response_model=AdminNotificationOut)
async def get_notification_admin(notification_id: int, service: Admin) -> AdminNotificationOut:
    return await service.get(notification_id)


@admin_router.patch("/{notification_id}", response_model=AdminNotificationOut)
async def update_notification(notification_id: int, data: AdminNotificationUpdate, service: Admin) -> AdminNotificationOut:
    """Only while scheduled (409 NOTIFICATION_NOT_EDITABLE otherwise)."""
    return await service.update(notification_id, data)


@admin_router.post("/{notification_id}/cancel", response_model=AdminNotificationOut)
async def cancel_notification(notification_id: int, service: Admin) -> AdminNotificationOut:
    return await service.cancel(notification_id)


@admin_router.post("/{notification_id}/send-now", response_model=AdminNotificationOut)
async def send_now(notification_id: int, service: Admin) -> AdminNotificationOut:
    """Send a scheduled notification right away (ignores quiet hours)."""
    return await service.send_now(notification_id)
