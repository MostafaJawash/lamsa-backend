"""Password hashing, password rules, and the API's own tokens.

Token types (the "type" claim is always checked, so one kind can never be used as another):
- access:         JWT, sub=user_id, role, fid (refresh family), iat, exp, jti
- google_signup:  JWT, sub=Google sub, email, name; lets a new Google user finish signup
- refresh:        opaque random string; only its SHA-256 hash is stored (RefreshToken table)

JWT iat/exp use real time (PyJWT validates them against the system clock).
"""

import asyncio
import hashlib
import secrets
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, Literal
from uuid import uuid4

import anyio
import jwt
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from app.core.config import settings
from app.shared.exceptions import ErrorCode, ServiceBusy, Unauthorized, ValidationFailed

TokenType = Literal["access", "google_signup"]

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128
REFRESH_TOKEN_BYTES = 32
TEMP_PASSWORD_LENGTH = 10
# No ambiguous characters (0/O/o, 1/l/I/i) so it can be read out over the phone.
TEMP_PASSWORD_LETTERS = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"
TEMP_PASSWORD_DIGITS = "23456789"

# ---------------------------------------------------------------------------- passwords

# Argon2id with the OWASP baseline: 19 MiB, 2 iterations, 1 lane (about 40 ms per hash on an
# 8-core laptop, and 19 MiB per concurrent operation instead of pwdlib's default 64 MiB). Hashes
# made with other parameters still verify and are re-hashed on the next successful login.
ARGON2_MEMORY_COST_KIB = 19456
ARGON2_TIME_COST = 2
ARGON2_PARALLELISM = 1


def build_password_hasher() -> PasswordHash:
    return PasswordHash(
        (Argon2Hasher(memory_cost=ARGON2_MEMORY_COST_KIB, time_cost=ARGON2_TIME_COST, parallelism=ARGON2_PARALLELISM),)
    )


password_hasher = build_password_hasher()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> tuple[bool, str | None]:
    """Return (valid, new_hash). new_hash is set when the stored hash uses outdated parameters."""
    try:
        return password_hasher.verify_and_update(password, password_hash)
    except Exception:
        return False, None


@lru_cache
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def verify_dummy_password(password: str) -> None:
    """Spend the same time as a real verification when there is no account to check."""
    verify_password(password, _dummy_hash())


# --- under load: every hash/verify goes through ONE limiter per process (and a worker thread) ---

_limiters: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def _limiter() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limiter = _limiters.get(loop)
    if limiter is None:
        limiter = _limiters[loop] = asyncio.Semaphore(settings.PASSWORD_HASH_CONCURRENCY)
    return limiter


def reset_hash_limiter() -> None:
    """Tests: forget the limiters (after changing PASSWORD_HASH_CONCURRENCY)."""
    _limiters.clear()


async def _limited(func: Any, *args: Any) -> Any:
    """Run a hash/verify in a worker thread, at most PASSWORD_HASH_CONCURRENCY at once. Waiting
    longer than PASSWORD_HASH_QUEUE_TIMEOUT raises 503 SERVICE_BUSY (Retry-After), so a login
    burst queues briefly and then sheds load instead of exhausting memory or CPU."""
    limiter = _limiter()
    try:
        await asyncio.wait_for(limiter.acquire(), timeout=settings.PASSWORD_HASH_QUEUE_TIMEOUT)
    except TimeoutError:
        raise ServiceBusy(retry_after=2) from None
    try:
        return await anyio.to_thread.run_sync(func, *args)
    finally:
        limiter.release()


async def hash_password_async(password: str) -> str:
    result: str = await _limited(hash_password, password)
    return result


async def verify_password_async(password: str, password_hash: str) -> tuple[bool, str | None]:
    result: tuple[bool, str | None] = await _limited(verify_password, password, password_hash)
    return result


async def verify_dummy_password_async(password: str) -> None:
    """Unknown phone: same cost AND same queue as a real verification (constant timing)."""
    await _limited(verify_dummy_password, password)


def validate_password(password: str, phone: str | None = None) -> None:
    """Raise WEAK_PASSWORD with a specific Arabic message if the password breaks a rule."""
    if len(password) < PASSWORD_MIN_LENGTH:
        message = f"كلمة المرور يجب أن تكون {PASSWORD_MIN_LENGTH} أحرف على الأقل."
    elif len(password) > PASSWORD_MAX_LENGTH:
        message = f"يجب ألا تتجاوز كلمة المرور {PASSWORD_MAX_LENGTH} حرفًا."
    elif password.isdigit():
        message = "يجب ألا تتكون كلمة المرور من أرقام فقط."
    elif phone and _matches_phone(password, phone):
        message = "يجب ألا تكون كلمة المرور رقم الهاتف."
    else:
        return
    raise ValidationFailed(ErrorCode.WEAK_PASSWORD, message, details=[{"field": "password", "message": message}])


def _matches_phone(password: str, phone: str) -> bool:
    """True if the password is the phone number in any format (+49 151..., 0151-..., 0049...)."""
    from app.shared.utils.phone import normalize_phone

    try:
        return normalize_phone(password) == phone
    except ValidationFailed:
        return False


def generate_temporary_password() -> str:
    """A readable temporary password that always passes validate_password."""
    alphabet = TEMP_PASSWORD_LETTERS + TEMP_PASSWORD_DIGITS
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(TEMP_PASSWORD_LENGTH))
        if any(c in TEMP_PASSWORD_LETTERS for c in password) and any(c in TEMP_PASSWORD_DIGITS for c in password):
            return password


# ------------------------------------------------------------------------- refresh tokens


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_family_id() -> str:
    return uuid4().hex


# -------------------------------------------------------------------------------- JWTs


def _encode(claims: dict[str, Any], lifetime: timedelta) -> str:
    now = datetime.now(UTC)
    payload = {**claims, "iat": now, "exp": now + lifetime, "jti": uuid4().hex}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """Decode and validate a JWT of the expected type; raise TOKEN_EXPIRED / TOKEN_INVALID."""
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["sub", "type", "iat", "exp", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized(ErrorCode.TOKEN_EXPIRED) from exc
    except jwt.PyJWTError as exc:
        raise Unauthorized(ErrorCode.TOKEN_INVALID) from exc
    if claims.get("type") != expected_type:
        raise Unauthorized(ErrorCode.TOKEN_INVALID)
    return claims


@dataclass(frozen=True)
class AccessClaims:
    user_id: int
    role: str
    family_id: str | None
    token_id: str


def create_access_token(
    user_id: int, role: str, family_id: str | None, lifetime: timedelta | None = None
) -> str:
    claims = {"sub": str(user_id), "role": role, "fid": family_id, "type": "access"}
    return _encode(claims, lifetime or timedelta(minutes=settings.ACCESS_TOKEN_MINUTES))


def decode_access_token(token: str) -> AccessClaims:
    claims = decode_token(token, "access")
    try:
        user_id = int(claims["sub"])
    except (TypeError, ValueError) as exc:
        raise Unauthorized(ErrorCode.TOKEN_INVALID) from exc
    return AccessClaims(
        user_id=user_id, role=str(claims.get("role")), family_id=claims.get("fid"), token_id=claims["jti"]
    )


def peek_access_user_id(token: str) -> int | None:
    """User id of a correctly signed access token even if it has expired, else None.

    Used only by logout, where an expired access token still proves who is logging out.
    """
    try:
        claims = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_exp": False, "require": ["sub", "type"]},
        )
        if claims.get("type") != "access":
            return None
        return int(claims["sub"])
    except (jwt.PyJWTError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class SignupClaims:
    google_sub: str
    email: str | None
    name: str | None


def create_signup_token(
    google_sub: str, email: str | None, name: str | None, lifetime: timedelta | None = None
) -> str:
    claims = {"sub": google_sub, "email": email, "name": name, "type": "google_signup"}
    return _encode(claims, lifetime or timedelta(minutes=settings.SIGNUP_TOKEN_MINUTES))


def decode_signup_token(token: str) -> SignupClaims:
    claims = decode_token(token, "google_signup")
    return SignupClaims(google_sub=str(claims["sub"]), email=claims.get("email"), name=claims.get("name"))
