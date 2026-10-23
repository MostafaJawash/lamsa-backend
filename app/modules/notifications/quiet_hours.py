"""Quiet hours in the store's time zone (DST-aware via zoneinfo).

Only marketing, favorite_* and cart_reminder respect them; order updates and admin alerts never
wait. Anything due inside the window is deferred to the next quiet_hours_end.
"""

from datetime import UTC, datetime, time, timedelta

from app.modules.notifications.enums import QUIET_HOURS_TYPES, NotificationType
from app.modules.settings.service import NotificationSettings


def _in_window(local: time, start: time, end: time) -> bool:
    if start == end:
        return False
    if start < end:  # e.g. 13:00-15:00
        return start <= local < end
    return local >= start or local < end  # crosses midnight, e.g. 21:00-09:00


def deferred_until(due_utc: datetime, settings: NotificationSettings) -> datetime | None:
    """The next quiet_hours_end (naive UTC) if due_utc falls inside quiet hours, else None."""
    zone = settings.zone
    local = due_utc.replace(tzinfo=UTC).astimezone(zone)
    start, end = settings.quiet_hours_start, settings.quiet_hours_end
    if not _in_window(local.time(), start, end):
        return None
    end_day = local.date() if local.time() < end else local.date() + timedelta(days=1)
    end_local = datetime.combine(end_day, end, tzinfo=zone)  # zoneinfo applies that day's offset (DST)
    return end_local.astimezone(UTC).replace(tzinfo=None)


def effective_schedule(
    type_: NotificationType, due_utc: datetime, settings: NotificationSettings
) -> tuple[datetime, bool]:
    """(when to send, deferred_by_quiet_hours)."""
    if type_ in QUIET_HOURS_TYPES:
        later = deferred_until(due_utc, settings)
        if later is not None:
            return later, True
    return due_utc, False
