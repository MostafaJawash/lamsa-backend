from datetime import datetime

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.enums import RevokeReason
from app.modules.auth.models import LoginAttempt, RefreshToken

# ---------------------------------------------------------------------------- refresh tokens


async def get_refresh_token_by_hash(session: AsyncSession, token_hash: str) -> RefreshToken | None:
    """Fetch and row-lock the token, so concurrent refreshes of the same token run one by one."""
    return await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
    )


async def family_has_active_token(session: AsyncSession, family_id: str, now: datetime) -> bool:
    active = (
        select(RefreshToken.id)
        .where(
            RefreshToken.family_id == family_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )
        .limit(1)
    )
    return bool(await session.scalar(select(exists(active))))


async def revoke_family(
    session: AsyncSession, family_id: str, reason: RevokeReason, now: datetime
) -> None:
    """Revoke every still-active token in the family (earlier revocations keep their reason)."""
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason)
    )


async def revoke_user_tokens(
    session: AsyncSession,
    user_id: int,
    reason: RevokeReason,
    now: datetime,
    except_family_id: str | None = None,
) -> None:
    stmt = (
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason)
    )
    if except_family_id is not None:
        stmt = stmt.where(RefreshToken.family_id != except_family_id)
    await session.execute(stmt)


async def rotate_family_tokens(
    session: AsyncSession, family_id: str, replaced_by_id: int, now: datetime
) -> None:
    """Mark the family's other active tokens as rotated into replaced_by_id."""
    await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.family_id == family_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.id != replaced_by_id,
        )
        .values(revoked_at=now, revoked_reason=RevokeReason.rotated, replaced_by_id=replaced_by_id)
    )


async def family_belongs_to_user(session: AsyncSession, family_id: str, user_id: int) -> bool:
    match = select(RefreshToken.id).where(
        RefreshToken.family_id == family_id, RefreshToken.user_id == user_id
    )
    return bool(await session.scalar(select(exists(match.limit(1)))))


# ---------------------------------------------------------------------------- login attempts


def add_login_attempt(
    session: AsyncSession, phone: str, ip: str, succeeded: bool, now: datetime
) -> None:
    session.add(LoginAttempt(phone=phone, ip_address=ip, succeeded=succeeded, created_at=now))


async def failed_attempt_times(
    session: AsyncSession, *, since: datetime, phone: str | None = None, ip: str | None = None
) -> list[datetime]:
    """Times of failed attempts since `since` for the phone or IP, oldest first."""
    stmt = select(LoginAttempt.created_at).where(
        LoginAttempt.succeeded.is_(False), LoginAttempt.created_at >= since
    )
    if phone is not None:
        stmt = stmt.where(LoginAttempt.phone == phone)
    if ip is not None:
        stmt = stmt.where(LoginAttempt.ip_address == ip)
    return list((await session.scalars(stmt.order_by(LoginAttempt.created_at))).all())


async def delete_attempts_before(session: AsyncSession, before: datetime) -> int:
    result = await session.execute(delete(LoginAttempt).where(LoginAttempt.created_at < before))
    return int(result.rowcount or 0)


async def count_active_tokens(session: AsyncSession, user_id: int, now: datetime) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > now,
            )
        )
        or 0
    )
