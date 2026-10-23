from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, table_args
from app.db.mixins import CURRENT_TIMESTAMP, CreatedAtMixin, IdMixin
from app.db.types import NullableJSON, str_enum
from app.modules.notifications.enums import NotificationStatus, NotificationTarget, NotificationType
from app.shared.enums import LinkType

if TYPE_CHECKING:
    from app.modules.users.models import User


class Notification(IdMixin, CreatedAtMixin, Base):
    """A push/in-app notification for everyone (target=all) or one user (target=user).

    Enforced by the service layer (app.modules.notifications.service.new_notification), since
    MySQL forbids this CHECK on a column with an ON DELETE CASCADE foreign key:
    (target = 'user' AND user_id IS NOT NULL) OR (target = 'all' AND user_id IS NULL).

    Lifecycle (outbox): queued | scheduled -> sending (claimed by one dispatcher) -> sent;
    scheduled -> cancelled. A row is written first and pushed later, so nothing is lost.
    """

    __tablename__ = "notifications"
    __table_args__ = table_args(
        Index("ix_notifications_status_scheduled_at", "status", "scheduled_at"),
        Index("ix_notifications_target_sent_at", "target", "sent_at"),
        Index("ix_notifications_user_id_sent_at", "user_id", "sent_at"),  # inbox; also serves the FK
    )

    title: Mapped[str] = mapped_column(String(150))
    body: Mapped[str] = mapped_column(String(500))
    target: Mapped[NotificationTarget] = mapped_column(str_enum(NotificationTarget))
    type: Mapped[NotificationType] = mapped_column(str_enum(NotificationType, length=40))
    status: Mapped[NotificationStatus] = mapped_column(str_enum(NotificationStatus, length=20))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    recipients_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    success_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    failure_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    link_type: Mapped[LinkType] = mapped_column(
        str_enum(LinkType), default=LinkType.none, server_default=LinkType.none.value
    )
    link_value: Mapped[str | None] = mapped_column(String(255))
    data: Mapped[dict[str, Any] | None] = mapped_column(NullableJSON)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User | None] = relationship(foreign_keys=[user_id], lazy="raise")
    reads: Mapped[list[NotificationRead]] = relationship(
        back_populates="notification", lazy="raise", cascade="all, delete-orphan", passive_deletes=True
    )


class NotificationRead(Base):
    __tablename__ = "notification_reads"

    # notification_id is covered by the primary key (notification_id, user_id).
    notification_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("notifications.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    read_at: Mapped[datetime] = mapped_column(DateTime, server_default=CURRENT_TIMESTAMP)

    notification: Mapped[Notification] = relationship(back_populates="reads", lazy="raise")
    user: Mapped[User] = relationship(lazy="raise")


class UserAlertLog(IdMixin, CreatedAtMixin, Base):
    """Smart alerts already sent: cooldowns per (user, kind, product) and cart-reminder dedup."""

    __tablename__ = "user_alert_log"
    __table_args__ = table_args(
        Index("ix_user_alert_log_user_id_kind_ref_id_created_at", "user_id", "kind", "ref_id", "created_at"),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[NotificationType] = mapped_column(str_enum(NotificationType, length=40))
    ref_id: Mapped[int | None] = mapped_column(BigInteger)  # product id; NULL for cart reminders
