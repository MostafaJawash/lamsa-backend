"""Verify Google ID tokens sent by the apps after Google sign-in."""

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import anyio
import cachecontrol
import requests
from google.auth import exceptions as google_exceptions
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

from app.core.config import settings
from app.shared.exceptions import ErrorCode, Unauthorized

logger = logging.getLogger(__name__)

DEV_TOKEN_PREFIX = "dev-google|"


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str | None
    name: str | None


@lru_cache
def _google_request() -> GoogleRequest:
    # One shared session; CacheControl caches Google's signing certificates for as long as
    # their Cache-Control header allows, instead of downloading them on every login.
    session = cachecontrol.CacheControl(requests.Session())
    return GoogleRequest(session=session)


def _verify_with_google(token: str) -> dict[str, Any]:
    claims: dict[str, Any] = id_token.verify_oauth2_token(
        token, _google_request(), audience=settings.GOOGLE_CLIENT_IDS
    )
    return claims


def _parse_dev_token(token: str) -> GoogleIdentity:
    """dev-google|<sub>|<email>|<name>  (GOOGLE_DEV_MODE only)."""
    parts = token[len(DEV_TOKEN_PREFIX) :].split("|")
    if len(parts) != 3 or not parts[0].strip():
        raise Unauthorized(ErrorCode.GOOGLE_TOKEN_INVALID)
    sub, email, name = (part.strip() for part in parts)
    return GoogleIdentity(sub=f"dev-{sub}", email=email or None, name=name or None)


async def verify_google_id_token(token: str) -> GoogleIdentity:
    """Verify a Google ID token and return the identity, or raise GOOGLE_TOKEN_INVALID."""
    if settings.GOOGLE_DEV_MODE and token.startswith(DEV_TOKEN_PREFIX):
        return _parse_dev_token(token)

    if not settings.GOOGLE_CLIENT_IDS:
        logger.error("GOOGLE_CLIENT_IDS is empty; cannot verify Google sign-in")
        raise Unauthorized(ErrorCode.GOOGLE_TOKEN_INVALID)

    try:
        claims = await anyio.to_thread.run_sync(_verify_with_google, token)
    except (ValueError, google_exceptions.GoogleAuthError) as exc:
        logger.info("Rejected Google ID token: %s", exc)
        raise Unauthorized(ErrorCode.GOOGLE_TOKEN_INVALID) from exc

    if claims.get("email_verified") is not True or not claims.get("sub"):
        raise Unauthorized(ErrorCode.GOOGLE_TOKEN_INVALID)
    return GoogleIdentity(sub=str(claims["sub"]), email=claims.get("email"), name=claims.get("name"))
