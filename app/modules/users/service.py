"""Profile, phone change, account deletion, addresses and devices. One transaction per call."""

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock
from app.modules.auth.service import flush_user_changes, verify_password_async
from app.modules.users import repository as users_repo
from app.modules.users.enums import AuthProvider
from app.modules.users.models import User, UserAddress, UserDevice
from app.modules.users.schemas import (
    AddressCreate,
    AddressUpdate,
    DeviceRegister,
    PhoneChange,
    UserUpdate,
)
from app.shared.exceptions import Conflict, ErrorCode, NotFound, Unauthorized, ValidationFailed
from app.shared.utils.phone import normalize_phone

logger = logging.getLogger(__name__)

MAX_ADDRESSES = 10
DELETED_USER_NAME = "حساب محذوف"


async def confirm_password(user: User, password: str | None, session: AsyncSession | None = None) -> None:
    """Phone accounts must confirm sensitive changes with their current password."""
    if user.auth_provider != AuthProvider.phone or not user.password_hash:
        return
    if not password:
        raise ValidationFailed(ErrorCode.CURRENT_PASSWORD_REQUIRED)
    valid, _ = await verify_password_async(password, user.password_hash, release=session)
    if not valid:
        raise Unauthorized(ErrorCode.INVALID_CREDENTIALS)


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def update_profile(self, user: User, data: UserUpdate) -> User:
        changes = data.model_dump(exclude_unset=True)
        if "name" in changes and changes["name"] is None:
            changes.pop("name")  # name is required; null means "leave as is"
        for field, value in changes.items():
            setattr(user, field, value)
        await self.session.commit()
        return user

    async def change_phone(self, user: User, data: PhoneChange) -> User:
        phone = normalize_phone(data.phone)
        await confirm_password(user, data.current_password, self.session)
        if phone == user.phone:
            return user
        if await users_repo.phone_taken(self.session, phone, exclude_user_id=user.id):
            raise Conflict(ErrorCode.PHONE_TAKEN)
        user.phone = phone
        await flush_user_changes(self.session)
        await self.session.commit()
        return user

    async def delete_account(self, user: User, current_password: str | None) -> None:
        """Delete the account, or anonymise it if it has orders (orders keep their snapshots)."""
        await confirm_password(user, current_password, self.session)
        user_id = user.id
        if await users_repo.count_orders(self.session, user_id) == 0:
            await users_repo.delete_user(self.session, user_id)
            await self.session.commit()
            self.session.expunge(user)
            logger.info("Deleted account user=%s", user_id)
            return

        user.name = DELETED_USER_NAME
        user.phone = f"deleted-{user_id}"
        user.email = None
        user.google_sub = None
        user.password_hash = None
        user.must_change_password = False
        user.is_active = False
        await users_repo.delete_personal_data(self.session, user_id)
        await self.session.commit()
        logger.info("Anonymised account with orders user=%s", user_id)


class AddressService:
    """Rules: the first address is the default; setting a default unsets the others; deleting
    the default promotes the newest remaining one; at most 10 addresses per user."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _lock_user(self, user: User) -> None:
        # Serialises address changes of one user (default flag, limit) across requests.
        await users_repo.get_user(self.session, user.id, for_update=True)

    async def list(self, user: User) -> list[UserAddress]:
        return await users_repo.list_addresses(self.session, user.id)

    async def create(self, user: User, data: AddressCreate) -> UserAddress:
        await self._lock_user(user)
        count = await users_repo.count_addresses(self.session, user.id)
        if count >= MAX_ADDRESSES:
            raise ValidationFailed(ErrorCode.ADDRESS_LIMIT_REACHED)
        make_default = data.is_default or count == 0
        if make_default:
            await users_repo.clear_default_address(self.session, user.id)
        address = UserAddress(
            user_id=user.id,
            **data.model_dump(exclude={"is_default"}),
            is_default=make_default,
            created_at=clock.utcnow(),
        )
        self.session.add(address)
        await self.session.commit()
        return address

    async def update(self, user: User, address_id: int, data: AddressUpdate) -> UserAddress:
        await self._lock_user(user)
        address = await self._get(user, address_id)
        changes = data.model_dump(exclude_unset=True)
        make_default = changes.pop("is_default", None) is True
        for field in ("city", "street"):
            if field in changes and changes[field] is None:
                changes.pop(field)  # required columns: null means "leave as is"
        for field, value in changes.items():
            setattr(address, field, value)
        if make_default and not address.is_default:
            await users_repo.clear_default_address(self.session, user.id, except_id=address.id)
            address.is_default = True
        await self.session.commit()
        return address

    async def set_default(self, user: User, address_id: int) -> UserAddress:
        await self._lock_user(user)
        address = await self._get(user, address_id)
        if not address.is_default:
            await users_repo.clear_default_address(self.session, user.id, except_id=address.id)
            address.is_default = True
            await self.session.commit()
        return address

    async def delete(self, user: User, address_id: int) -> None:
        await self._lock_user(user)
        address = await self._get(user, address_id)
        was_default = address.is_default
        await self.session.delete(address)
        await self.session.flush()
        if was_default:
            replacement = await users_repo.newest_address(self.session, user.id)
            if replacement is not None:
                replacement.is_default = True
        await self.session.commit()

    async def _get(self, user: User, address_id: int) -> UserAddress:
        address = await users_repo.get_address(self.session, user.id, address_id)
        if address is None:
            raise NotFound(ErrorCode.ADDRESS_NOT_FOUND)
        return address


class DeviceService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def register(self, user: User, data: DeviceRegister) -> None:
        """Upsert by FCM token. A token seen on another account moves to this user
        (the device changed hands or the user switched accounts)."""
        now = clock.utcnow()
        device = await users_repo.get_device_by_token(self.session, data.fcm_token)
        if device is None:
            self.session.add(
                UserDevice(
                    user_id=user.id, fcm_token=data.fcm_token, platform=data.platform,
                    last_seen_at=now, created_at=now,
                )
            )
            try:
                await self.session.commit()
                return
            except IntegrityError:
                # A parallel request inserted the same token first: update that row instead.
                await self.session.rollback()
                device = await users_repo.get_device_by_token(self.session, data.fcm_token)
                if device is None:
                    raise
        device.user_id = user.id
        device.platform = data.platform
        device.last_seen_at = now
        await self.session.commit()

    async def unregister(self, user: User, fcm_token: str) -> None:
        await users_repo.delete_device(self.session, user.id, fcm_token)
        await self.session.commit()
