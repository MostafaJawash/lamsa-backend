from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Numeric, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, table_args
from app.db.mixins import CURRENT_TIMESTAMP, CreatedAtMixin, IdMixin, TimestampMixin
from app.db.types import str_enum
from app.modules.users.enums import AuthProvider, DevicePlatform, UserRole

if TYPE_CHECKING:
    from app.modules.auth.models import RefreshToken
    from app.modules.cart.models import CartItem
    from app.modules.favorites.models import Favorite
    from app.modules.orders.models import Order


class User(IdMixin, TimestampMixin, Base):
    """A customer or admin.

    A row is created only once the user has a phone number: Google sign-in users
    complete their profile (name + phone) first, so phone stays NOT NULL. The phone is not
    verified (no SMS); one account per phone, conflicts are resolved by store support.

    Account kinds (enforced by the service layer, not the database):
    - auth_provider "phone": has password_hash, google_sub is NULL. Logs in with phone + password.
    - auth_provider "google": has google_sub, password_hash is NULL. Logs in with Google.

    must_change_password is set when an admin issues a temporary password; until the user
    changes it, only a few endpoints are allowed (see app.shared.dependencies.auth).
    """

    __tablename__ = "users"

    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True)  # Google "sub" claim
    password_hash: Mapped[str | None] = mapped_column(String(255))  # Argon2, phone accounts only
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0")
    )
    name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str] = mapped_column(String(20), unique=True)  # E.164, e.g. +963933123456
    email: Mapped[str | None] = mapped_column(String(255))
    auth_provider: Mapped[AuthProvider] = mapped_column(str_enum(AuthProvider))
    role: Mapped[UserRole] = mapped_column(
        str_enum(UserRole), default=UserRole.customer, server_default=UserRole.customer.value
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Notification preferences (order updates cannot be switched off).
    notify_marketing: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    notify_favorites: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    notify_cart_reminders: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    # Set by EVERY cart write (deletions too): drives cart reminders.
    cart_changed_at: Mapped[datetime | None] = mapped_column(DateTime)

    addresses: Mapped[list[UserAddress]] = relationship(
        back_populates="user", lazy="raise", cascade="all, delete-orphan", passive_deletes=True
    )
    devices: Mapped[list[UserDevice]] = relationship(
        back_populates="user", lazy="raise", cascade="all, delete-orphan", passive_deletes=True
    )
    cart_items: Mapped[list[CartItem]] = relationship(
        back_populates="user", lazy="raise", cascade="all, delete-orphan", passive_deletes=True
    )
    favorites: Mapped[list[Favorite]] = relationship(
        back_populates="user", lazy="raise", cascade="all, delete-orphan", passive_deletes=True
    )
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user", lazy="raise", cascade="all, delete-orphan", passive_deletes=True
    )
    # orders.user_id is ON DELETE RESTRICT: never cascade, never null out from the ORM.
    orders: Mapped[list[Order]] = relationship(
        back_populates="user", lazy="raise", passive_deletes="all"
    )


class UserAddress(IdMixin, TimestampMixin, Base):
    __tablename__ = "user_addresses"
    __table_args__ = table_args(
        CheckConstraint(
            "governorate IN ('damascus', 'rif_dimashq', 'aleppo', 'homs', 'hama', "
            "'latakia', 'tartus', 'idlib', 'deir_ez_zor', 'raqqa', 'hasakah', "
            "'daraa', 'as_suwayda', 'quneitra')",
            name="governorate_supported",
        ),
        CheckConstraint(
            "((latitude IS NULL AND longitude IS NULL) OR "
            "(latitude IS NOT NULL AND longitude IS NOT NULL AND "
            "latitude BETWEEN 32.300000 AND 37.400000 AND "
            "longitude BETWEEN 35.600000 AND 42.400000))",
            name="location_in_syria",
        ),
        CheckConstraint("CHAR_LENGTH(details) BETWEEN 10 AND 500", name="details_length"),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    label: Mapped[str | None] = mapped_column(String(50))  # "البيت", "الشغل"
    governorate: Mapped[str] = mapped_column(String(32))
    area: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[str] = mapped_column(String(500))
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    notes: Mapped[str | None] = mapped_column(String(500))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))

    user: Mapped[User] = relationship(back_populates="addresses", lazy="raise")


class UserDevice(IdMixin, CreatedAtMixin, Base):
    """An FCM push token.

    fcm_token has a plain UNIQUE index: VARCHAR(512) in utf8mb4 is at most 2048 bytes,
    within InnoDB's 3072-byte key limit, so no prefix index or hash column is needed.
    """

    __tablename__ = "user_devices"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    fcm_token: Mapped[str] = mapped_column(String(512), unique=True)
    platform: Mapped[DevicePlatform] = mapped_column(str_enum(DevicePlatform))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=CURRENT_TIMESTAMP)

    user: Mapped[User] = relationship(back_populates="devices", lazy="raise")
