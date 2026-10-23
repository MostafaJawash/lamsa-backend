"""Authentication dependencies.

Use `CurrentUser` on normal endpoints. It enforces the PASSWORD_CHANGE_REQUIRED gate: a user
with a temporary password may only call GET /users/me, POST /auth/password/change and
POST /auth/logout (those use `CurrentUserAllowPasswordChange` / `AuthContextAllowPasswordChange`).
POST /auth/refresh does not use an access token at all.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import AccessClaims, decode_access_token
from app.db.session import get_db
from app.modules.users.enums import UserRole
from app.modules.users.models import User
from app.shared.exceptions import ErrorCode, Forbidden, Unauthorized

bearer_scheme = HTTPBearer(auto_error=False, description="Access token from /auth/login")

DbSession = Annotated[AsyncSession, Depends(get_db)]
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


@dataclass(frozen=True)
class AuthContext:
    user: User
    claims: AccessClaims


async def _authenticate(credentials: HTTPAuthorizationCredentials | None, session: AsyncSession) -> AuthContext:
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise Unauthorized(ErrorCode.NOT_AUTHENTICATED)
    claims = decode_access_token(credentials.credentials)
    user = await session.get(User, claims.user_id)
    if user is None:
        raise Unauthorized(ErrorCode.USER_NOT_FOUND)
    if not user.is_active:
        raise Forbidden(ErrorCode.ACCOUNT_DISABLED)
    return AuthContext(user=user, claims=claims)


async def get_auth_context_allow_password_change(
    credentials: BearerCredentials, session: DbSession
) -> AuthContext:
    return await _authenticate(credentials, session)


async def get_auth_context(credentials: BearerCredentials, session: DbSession) -> AuthContext:
    context = await _authenticate(credentials, session)
    if context.user.must_change_password:
        raise Forbidden(ErrorCode.PASSWORD_CHANGE_REQUIRED)
    return context


async def get_current_user(context: Annotated[AuthContext, Depends(get_auth_context)]) -> User:
    return context.user


async def get_current_user_allow_password_change(
    context: Annotated[AuthContext, Depends(get_auth_context_allow_password_change)],
) -> User:
    return context.user


async def get_optional_user(
    request: Request, credentials: BearerCredentials, session: DbSession
) -> User | None:
    """For public endpoints that personalise their answer (e.g. is_favorite).

    No Authorization header: anonymous (None). A header that is present but not a valid access
    token: 401 as usual, so the app refreshes its token. The PASSWORD_CHANGE_REQUIRED gate does
    not apply here: browsing stays possible with a temporary password.
    """
    if "authorization" not in request.headers:
        return None
    return (await _authenticate(credentials, session)).user


async def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    if user.role != UserRole.admin:
        raise Forbidden(ErrorCode.ADMIN_ONLY)
    return user


AuthContextDep = Annotated[AuthContext, Depends(get_auth_context)]
AuthContextAllowPasswordChange = Annotated[AuthContext, Depends(get_auth_context_allow_password_change)]
CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentUserAllowPasswordChange = Annotated[User, Depends(get_current_user_allow_password_change)]
AdminUser = Annotated[User, Depends(require_admin)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
