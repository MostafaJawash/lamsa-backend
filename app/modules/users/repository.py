from sqlalchemy import ScalarSelect, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import RefreshToken
from app.modules.cart.models import CartItem
from app.modules.favorites.models import Favorite
from app.modules.orders.models import Order
from app.modules.users.enums import UserRole
from app.modules.users.models import User, UserAddress, UserDevice

# ------------------------------------------------------------------------------------ users


async def get_user(session: AsyncSession, user_id: int, *, for_update: bool = False) -> User | None:
    stmt = select(User).where(User.id == user_id)
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def get_user_by_phone(session: AsyncSession, phone: str) -> User | None:
    return await session.scalar(select(User).where(User.phone == phone))


async def get_user_by_google_sub(session: AsyncSession, google_sub: str) -> User | None:
    return await session.scalar(select(User).where(User.google_sub == google_sub))


async def phone_taken(session: AsyncSession, phone: str, *, exclude_user_id: int | None = None) -> bool:
    stmt = select(User.id).where(User.phone == phone)
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)
    return await session.scalar(stmt.limit(1)) is not None


def orders_count_subquery() -> ScalarSelect[int]:
    return (
        select(func.count(Order.id)).where(Order.user_id == User.id).correlate(User).scalar_subquery()
    )


async def count_orders(session: AsyncSession, user_id: int) -> int:
    return int(await session.scalar(select(func.count(Order.id)).where(Order.user_id == user_id)) or 0)


async def delete_user(session: AsyncSession, user_id: int) -> None:
    """Delete the row; the database cascades addresses, devices, cart, favorites,
    notification reads and refresh tokens."""
    await session.execute(delete(User).where(User.id == user_id))


async def delete_personal_data(session: AsyncSession, user_id: int) -> None:
    for model in (UserAddress, UserDevice, CartItem, Favorite, RefreshToken):
        await session.execute(delete(model).where(model.user_id == user_id))


async def list_users(
    session: AsyncSession,
    *,
    search: str | None,
    role: UserRole | None,
    is_active: bool | None,
    offset: int,
    limit: int,
) -> tuple[list[tuple[User, int]], int]:
    filters = []
    if search:
        filters.append(
            User.name.contains(search, autoescape=True) | User.phone.contains(search, autoescape=True)
        )
    if role is not None:
        filters.append(User.role == role)
    if is_active is not None:
        filters.append(User.is_active.is_(is_active))

    total = int(await session.scalar(select(func.count(User.id)).where(*filters)) or 0)
    rows = await session.execute(
        select(User, orders_count_subquery().label("orders_count"))
        .where(*filters)
        .order_by(User.created_at.desc(), User.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return [(user, int(count)) for user, count in rows.all()], total


# -------------------------------------------------------------------------------- addresses


async def list_addresses(session: AsyncSession, user_id: int) -> list[UserAddress]:
    result = await session.scalars(
        select(UserAddress)
        .where(UserAddress.user_id == user_id)
        .order_by(UserAddress.is_default.desc(), UserAddress.created_at.desc(), UserAddress.id.desc())
    )
    return list(result.all())


async def get_address(session: AsyncSession, user_id: int, address_id: int) -> UserAddress | None:
    return await session.scalar(
        select(UserAddress).where(UserAddress.id == address_id, UserAddress.user_id == user_id)
    )


async def count_addresses(session: AsyncSession, user_id: int) -> int:
    return int(
        await session.scalar(select(func.count(UserAddress.id)).where(UserAddress.user_id == user_id)) or 0
    )


async def clear_default_address(session: AsyncSession, user_id: int, *, except_id: int | None = None) -> None:
    stmt = update(UserAddress).where(UserAddress.user_id == user_id, UserAddress.is_default.is_(True))
    if except_id is not None:
        stmt = stmt.where(UserAddress.id != except_id)
    await session.execute(stmt.values(is_default=False).execution_options(synchronize_session="fetch"))


async def newest_address(session: AsyncSession, user_id: int) -> UserAddress | None:
    return await session.scalar(
        select(UserAddress)
        .where(UserAddress.user_id == user_id)
        .order_by(UserAddress.created_at.desc(), UserAddress.id.desc())
        .limit(1)
    )


# ---------------------------------------------------------------------------------- devices


async def get_device_by_token(session: AsyncSession, fcm_token: str) -> UserDevice | None:
    return await session.scalar(
        select(UserDevice).where(UserDevice.fcm_token == fcm_token).with_for_update()
    )


async def delete_device(session: AsyncSession, user_id: int, fcm_token: str) -> None:
    await session.execute(
        delete(UserDevice).where(UserDevice.user_id == user_id, UserDevice.fcm_token == fcm_token)
    )
