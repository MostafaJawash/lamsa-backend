from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CHAR, BigInteger, Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, table_args
from app.db.mixins import CreatedAtMixin, IdMixin
from app.db.types import str_enum
from app.modules.auth.enums import RevokeReason

if TYPE_CHECKING:
    from app.modules.users.models import User


class RefreshToken(IdMixin, CreatedAtMixin, Base):
    """A rotating refresh token. Only the SHA-256 hash of the token is stored.

    All tokens issued from one login share family_id. Refreshing revokes the presented token
    (reason "rotated") and links it to its successor via replaced_by_id. Presenting a revoked
    token again revokes the whole family (reuse detection), except for a short grace period
    after rotation (see AuthService.refresh).
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(CHAR(64), unique=True)
    family_id: Mapped[str] = mapped_column(CHAR(32), index=True)
    device_name: Mapped[str | None] = mapped_column(String(100))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_reason: Mapped[RevokeReason | None] = mapped_column(str_enum(RevokeReason))
    replaced_by_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("refresh_tokens.id", ondelete="SET NULL"), index=True
    )

    user: Mapped[User] = relationship(back_populates="refresh_tokens", lazy="raise")


class LoginAttempt(IdMixin, CreatedAtMixin, Base):
    """One password login attempt, used for rate limiting (only failures count)."""

    __tablename__ = "login_attempts"
    __table_args__ = table_args(
        Index("ix_login_attempts_phone_created_at", "phone", "created_at"),
        Index("ix_login_attempts_ip_address_created_at", "ip_address", "created_at"),
    )

    phone: Mapped[str] = mapped_column(String(20))
    ip_address: Mapped[str] = mapped_column(String(45))
    succeeded: Mapped[bool] = mapped_column(Boolean)
