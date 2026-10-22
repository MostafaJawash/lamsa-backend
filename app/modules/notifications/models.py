from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import CURRENT_TIMESTAMP, CreatedAtMixin, IdMixin
from app.db.types import NullableJSON, str_enum
from app.modules.notifications.enums import NotificationTarget
from app.shared.enums import LinkType

if TYPE_CHECKING:
    from app.modules.users.models import User


class Notification(IdMixin, CreatedAtMixin, Base):
    """A push/in-app notification for everyone (target=all) or one user (target=user).

    TODO(service layer): enforce (target = 'user' AND user_id IS NOT NULL) OR
    (target = 'all' AND user_id IS NULL). MySQL forbids this CHECK because user_id has an
    ON DELETE CASCADE foreign key.
    """

    __tablename__ = "notifications"

    title: Mapped[str] = mapped_column(String(150))
    body: Mapped[str] = mapped_column(String(500))
    target: Mapped[NotificationTarget] = mapped_column(str_enum(NotificationTarget))
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    link_type: Mapped[LinkType] = mapped_column(
        str_enum(LinkType), default=LinkType.none, server_default=LinkType.none.value
    )
    link_value: Mapped[str | None] = mapped_column(String(255))
    data: Mapped[dict[str, Any] | None] = mapped_column(NullableJSON)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User | None] = relationship(lazy="raise")
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
