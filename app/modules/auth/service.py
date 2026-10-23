"""Auth business rules: registration, login, Google sign-in, token refresh/rotation, logout,
password change. Each public method is one transaction and commits itself."""

import logging
import math
import random
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import release_read_transaction
from app.core import clock, security
from app.core.config import settings
from app.integrations.google import auth as google_auth
from app.modules.auth import repository as auth_repo
from app.modules.auth.enums import RevokeReason
from app.modules.auth.models import RefreshToken
from app.modules.auth.schemas import (
    GoogleCompleteRequest,
    GoogleLoginRequest,
    GoogleLoginResponse,
    GoogleProfile,
    LoginRequest,
    PasswordChangeRequest,
    RegisterRequest,
    TokenPair,
)
from app.modules.users import repository as users_repo
from app.modules.users.enums import AuthProvider
from app.modules.users.models import User
from app.modules.users.schemas import UserOut
from app.shared.dependencies.auth import AuthContext
from app.shared.dependencies.request import ClientInfo
from app.shared.exceptions import (
    Conflict,
    ErrorCode,
    Forbidden,
    TooManyRequests,
    Unauthorized,
    ValidationFailed,
)
from app.shared.utils.phone import normalize_phone

logger = logging.getLogger(__name__)

LOGIN_WINDOW = timedelta(minutes=15)
MAX_FAILED_PER_PHONE = 5
MAX_FAILED_PER_IP = 20
ATTEMPT_RETENTION = timedelta(days=7)
CLEANUP_PROBABILITY = 0.01
LOGIN_PHONE_COLUMN_LENGTH = 20


# ------------------------------------------------------------------------- shared helpers


def map_user_integrity_error(exc: IntegrityError) -> Conflict | None:
    """Translate a UNIQUE violation on users into the right 409 (registration races)."""
    text = str(exc.orig)
    if "uq_users_phone" in text:
        return Conflict(ErrorCode.PHONE_TAKEN)
    if "uq_users_google_sub" in text:
        return Conflict(ErrorCode.ALREADY_REGISTERED)
    return None


async def flush_user_changes(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        conflict = map_user_integrity_error(exc)
        if conflict is None:
            raise
        raise conflict from exc


async def release_connection(session: AsyncSession | None) -> None:
    """Free the pooled connection while the request queues for (and spends ~40 ms in) password
    hashing: otherwise a login burst holds every connection and unrelated requests time out."""
    await release_read_transaction(session)


async def hash_password_async(password: str, *, release: AsyncSession | None = None) -> str:
    """Limited and threaded (see security._limited). release: see release_connection()."""
    await release_connection(release)
    return await security.hash_password_async(password)


async def verify_password_async(
    password: str, password_hash: str, *, release: AsyncSession | None = None
) -> tuple[bool, str | None]:
    await release_connection(release)
    return await security.verify_password_async(password, password_hash)


async def issue_tokens(
    session: AsyncSession,
    user: User,
    client: ClientInfo,
    now: datetime,
    *,
    device_name: str | None = None,
    family_id: str | None = None,
) -> tuple[TokenPair, RefreshToken]:
    """Create a refresh token (new family unless given) and an access token for the user."""
    raw = security.generate_refresh_token()
    token = RefreshToken(
        user_id=user.id,
        token_hash=security.hash_token(raw),
        family_id=family_id or security.new_family_id(),
        device_name=device_name,
        user_agent=client.user_agent,
        ip_address=client.ip_address,
        expires_at=now + timedelta(days=settings.REFRESH_TOKEN_DAYS),
        created_at=now,
    )
    session.add(token)
    await session.flush()
    pair = TokenPair(
        access_token=security.create_access_token(user.id, user.role.value, token.family_id),
        refresh_token=raw,
        expires_in=settings.ACCESS_TOKEN_MINUTES * 60,
        user=UserOut.model_validate(user),
    )
    return pair, token


class AuthService:
    def __init__(self, session: AsyncSession, client: ClientInfo) -> None:
        self.session = session
        self.client = client

    # --------------------------------------------------------------------- registration

    async def register(self, data: RegisterRequest) -> TokenPair:
        phone = normalize_phone(data.phone)
        security.validate_password(data.password, phone)
        if await users_repo.phone_taken(self.session, phone):
            raise Conflict(ErrorCode.PHONE_TAKEN)

        now = clock.utcnow()
        user = User(
            name=data.name,
            phone=phone,
            auth_provider=AuthProvider.phone,
            password_hash=await hash_password_async(data.password, release=self.session),
            last_login_at=now,
        )
        self.session.add(user)
        await flush_user_changes(self.session)
        pair, _ = await issue_tokens(self.session, user, self.client, now, device_name=data.device_name)
        await self.session.commit()
        return pair

    # ---------------------------------------------------------------------------- login

    async def login(self, data: LoginRequest) -> TokenPair:
        now = clock.utcnow()
        ip = self.client.ip_address
        await self._enforce_limit(
            await auth_repo.failed_attempt_times(self.session, since=now - LOGIN_WINDOW, ip=ip),
            MAX_FAILED_PER_IP,
            now,
        )
        try:
            phone = normalize_phone(data.phone)
        except ValidationFailed:
            await self._record_attempt(data.phone.strip()[:LOGIN_PHONE_COLUMN_LENGTH], False, now)
            raise
        await self._enforce_limit(
            await auth_repo.failed_attempt_times(self.session, since=now - LOGIN_WINDOW, phone=phone),
            MAX_FAILED_PER_PHONE,
            now,
        )
        if random.random() < CLEANUP_PROBABILITY:
            await cleanup_login_attempts(self.session, now)

        user = await users_repo.get_user_by_phone(self.session, phone)
        new_hash: str | None = None
        if user is not None and user.password_hash:
            valid, new_hash = await verify_password_async(data.password, user.password_hash, release=self.session)
        else:
            # Same cost (and the same queue) as a real check, so response time does not reveal
            # unknown phones or Google accounts.
            await release_connection(self.session)
            await security.verify_dummy_password_async(data.password)
            valid = False

        if not valid or user is None:
            await self._record_attempt(phone, False, now)
            raise Unauthorized(ErrorCode.INVALID_CREDENTIALS)
        if not user.is_active:
            await self._record_attempt(phone, False, now)
            raise Forbidden(ErrorCode.ACCOUNT_DISABLED)

        if new_hash:
            user.password_hash = new_hash
        user.last_login_at = now
        auth_repo.add_login_attempt(self.session, phone, ip, True, now)
        pair, _ = await issue_tokens(self.session, user, self.client, now, device_name=data.device_name)
        await self.session.commit()
        return pair

    async def _record_attempt(self, phone: str, succeeded: bool, now: datetime) -> None:
        auth_repo.add_login_attempt(self.session, phone, self.client.ip_address, succeeded, now)
        await self.session.commit()

    @staticmethod
    async def _enforce_limit(failures: list[datetime], limit: int, now: datetime) -> None:
        if len(failures) < limit:
            return
        # Wait until enough failures leave the window to drop below the limit.
        unlock_at = failures[len(failures) - limit] + LOGIN_WINDOW
        raise TooManyRequests(ErrorCode.TOO_MANY_ATTEMPTS, retry_after=math.ceil((unlock_at - now).total_seconds()))

    # --------------------------------------------------------------------------- Google

    async def google_login(self, data: GoogleLoginRequest) -> GoogleLoginResponse:
        identity = await google_auth.verify_google_id_token(data.id_token)
        user = await users_repo.get_user_by_google_sub(self.session, identity.sub)
        if user is not None:
            if not user.is_active:
                raise Forbidden(ErrorCode.ACCOUNT_DISABLED)
            now = clock.utcnow()
            user.last_login_at = now
            pair, _ = await issue_tokens(self.session, user, self.client, now, device_name=data.device_name)
            await self.session.commit()
            return GoogleLoginResponse(registered=True, tokens=pair)

        return GoogleLoginResponse(
            registered=False,
            signup_token=security.create_signup_token(identity.sub, identity.email, identity.name),
            profile=GoogleProfile(email=identity.email, name=identity.name),
        )

    async def google_complete(self, data: GoogleCompleteRequest) -> TokenPair:
        claims = security.decode_signup_token(data.signup_token)
        if await users_repo.get_user_by_google_sub(self.session, claims.google_sub) is not None:
            raise Conflict(ErrorCode.ALREADY_REGISTERED)
        phone = normalize_phone(data.phone)
        if await users_repo.phone_taken(self.session, phone):
            raise Conflict(ErrorCode.PHONE_TAKEN)

        now = clock.utcnow()
        user = User(
            name=data.name,
            phone=phone,
            email=claims.email,
            auth_provider=AuthProvider.google,
            google_sub=claims.google_sub,
            last_login_at=now,
        )
        self.session.add(user)
        await flush_user_changes(self.session)
        pair, _ = await issue_tokens(self.session, user, self.client, now, device_name=data.device_name)
        await self.session.commit()
        return pair

    # --------------------------------------------------------------------------- tokens

    async def refresh(self, raw_token: str) -> TokenPair:
        now = clock.utcnow()
        token = await auth_repo.get_refresh_token_by_hash(self.session, security.hash_token(raw_token))
        if token is None:
            raise Unauthorized(ErrorCode.TOKEN_INVALID)

        if token.revoked_at is not None:
            if self._in_rotation_grace(token, now) and await auth_repo.family_has_active_token(
                self.session, token.family_id, now
            ):
                # Benign duplicate (e.g. parallel requests on app start): issue a sibling.
                user = await self._active_user(token.user_id)
                user.last_login_at = now
                pair, sibling = await issue_tokens(
                    self.session, user, self.client, now, device_name=token.device_name, family_id=token.family_id
                )
                await self.session.commit()
                logger.info(
                    "Duplicate refresh within grace period: user=%s family=%s token=%s sibling=%s",
                    token.user_id, token.family_id, token.id, sibling.id,
                )
                return pair

            await auth_repo.revoke_family(self.session, token.family_id, RevokeReason.reuse_detected, now)
            await self.session.commit()
            logger.warning(
                "Refresh token reuse detected: user=%s family=%s token=%s reason=%s",
                token.user_id, token.family_id, token.id, token.revoked_reason,
            )
            raise Unauthorized(ErrorCode.TOKEN_REUSED)

        if token.expires_at <= now:
            raise Unauthorized(ErrorCode.TOKEN_INVALID)

        user = await self._active_user(token.user_id)
        pair, successor = await issue_tokens(
            self.session, user, self.client, now, device_name=token.device_name, family_id=token.family_id
        )
        token.revoked_at = now
        token.revoked_reason = RevokeReason.rotated
        token.replaced_by_id = successor.id
        token.last_used_at = now
        user.last_login_at = now
        await self.session.commit()
        return pair

    @staticmethod
    def _in_rotation_grace(token: RefreshToken, now: datetime) -> bool:
        return (
            token.revoked_reason == RevokeReason.rotated
            and token.replaced_by_id is not None
            and token.revoked_at is not None
            and (now - token.revoked_at).total_seconds() < settings.REFRESH_REUSE_GRACE_SECONDS
        )

    async def _active_user(self, user_id: int) -> User:
        user = await users_repo.get_user(self.session, user_id)
        if user is None:
            raise Unauthorized(ErrorCode.TOKEN_INVALID)
        if not user.is_active:
            raise Forbidden(ErrorCode.ACCOUNT_DISABLED)
        return user

    async def logout(self, raw_token: str, access_token: str | None) -> None:
        """Revoke the token's family. If an access token is sent, it must be the same user's."""
        token = await auth_repo.get_refresh_token_by_hash(self.session, security.hash_token(raw_token))
        if token is None:
            return
        if access_token is not None and security.peek_access_user_id(access_token) != token.user_id:
            return
        await auth_repo.revoke_family(self.session, token.family_id, RevokeReason.logout, clock.utcnow())
        await self.session.commit()

    async def logout_all(self, user: User) -> None:
        await auth_repo.revoke_user_tokens(self.session, user.id, RevokeReason.logout_all, clock.utcnow())
        await self.session.commit()

    # ------------------------------------------------------------------------- password

    async def change_password(self, context: AuthContext, data: PasswordChangeRequest) -> TokenPair:
        user = context.user
        if user.auth_provider != AuthProvider.phone or not user.password_hash:
            raise ValidationFailed(ErrorCode.NOT_A_PASSWORD_ACCOUNT)
        valid, _ = await verify_password_async(data.current_password, user.password_hash, release=self.session)
        if not valid:
            raise Unauthorized(ErrorCode.INVALID_CREDENTIALS)
        same, _ = await verify_password_async(data.new_password, user.password_hash, release=self.session)
        if same:
            raise ValidationFailed(ErrorCode.SAME_PASSWORD)
        security.validate_password(data.new_password, user.phone)

        now = clock.utcnow()
        user.password_hash = await hash_password_async(data.new_password, release=self.session)
        user.must_change_password = False

        family_id = context.claims.family_id
        if family_id and not await auth_repo.family_belongs_to_user(self.session, family_id, user.id):
            family_id = None
        await auth_repo.revoke_user_tokens(
            self.session, user.id, RevokeReason.password_change, now, except_family_id=family_id
        )
        pair, token = await issue_tokens(self.session, user, self.client, now, family_id=family_id)
        if family_id:
            await auth_repo.rotate_family_tokens(self.session, family_id, token.id, now)
        await self.session.commit()
        return pair


async def cleanup_login_attempts(session: AsyncSession, now: datetime | None = None) -> int:
    """Delete login attempts older than 7 days (called opportunistically from login)."""
    now = now or clock.utcnow()
    return await auth_repo.delete_attempts_before(session, now - ATTEMPT_RETENTION)
