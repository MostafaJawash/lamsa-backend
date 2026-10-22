from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from app.core import security
from app.core.config import Settings
from app.integrations.google import auth as google_auth
from app.shared.exceptions import AppError, ErrorCode
from app.shared.utils.phone import normalize_phone

GOOD_SECRET = "s" * 48


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "DB_NAME": "x_test", "DB_USER": "u", "DB_PASSWORD": "p", "JWT_SECRET": GOOD_SECRET, **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


# ------------------------------------------------------------------------------- startup


def test_short_jwt_secret_refuses_to_start() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET must be at least 32 bytes"):
        make_settings(JWT_SECRET="too-short")


def test_google_dev_mode_only_in_development() -> None:
    with pytest.raises(ValidationError, match="GOOGLE_DEV_MODE"):
        make_settings(GOOGLE_DEV_MODE=True, APP_ENV="production")
    assert make_settings(GOOGLE_DEV_MODE=True, APP_ENV="development").GOOGLE_DEV_MODE is True


def test_comma_separated_lists() -> None:
    settings = make_settings(GOOGLE_CLIENT_IDS=" web.apps , android.apps,", CORS_ORIGINS="https://a.example")
    assert settings.GOOGLE_CLIENT_IDS == ["web.apps", "android.apps"]
    assert settings.CORS_ORIGINS == ["https://a.example"]
    assert make_settings(GOOGLE_CLIENT_IDS="").GOOGLE_CLIENT_IDS == []


# -------------------------------------------------------------------------------- tokens


def test_token_types_cannot_be_swapped() -> None:
    access = security.create_access_token(1, "customer", "f" * 32)
    signup = security.create_signup_token("sub", "e@example.com", "N")

    assert security.decode_access_token(access).user_id == 1
    assert security.decode_signup_token(signup).google_sub == "sub"
    for decode, token in ((security.decode_access_token, signup), (security.decode_signup_token, access)):
        with pytest.raises(AppError) as exc:
            decode(token)
        assert exc.value.code == ErrorCode.TOKEN_INVALID


def test_tampered_and_expired_tokens() -> None:
    access = security.create_access_token(1, "customer", None)
    with pytest.raises(AppError) as exc:
        security.decode_access_token(access[:-2] + ("A" if access[-2] != "A" else "B") + access[-1])
    assert exc.value.code == ErrorCode.TOKEN_INVALID

    expired = security.create_access_token(1, "customer", None, lifetime=timedelta(seconds=-1))
    with pytest.raises(AppError) as exc:
        security.decode_access_token(expired)
    assert exc.value.code == ErrorCode.TOKEN_EXPIRED


def test_refresh_token_hashing() -> None:
    token = security.generate_refresh_token()
    assert len(token) >= 43  # 32 random bytes, url-safe base64
    digest = security.hash_token(token)
    assert len(digest) == 64 and digest == security.hash_token(token)


def test_temporary_password_is_readable_and_valid() -> None:
    for _ in range(200):
        password = security.generate_temporary_password()
        assert len(password) == 10
        assert not set(password) & set("0O1lIio")
        security.validate_password(password, "+4915123456789")


# --------------------------------------------------------------------------------- phone


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("015123456789", "+4915123456789"),
        ("+49 (151) 234-567-89", "+4915123456789"),
        ("0049 151 23456789", "+4915123456789"),
        ("+962 79 123 4567", "+962791234567"),
    ],
)
def test_normalize_phone(raw: str, expected: str) -> None:
    assert normalize_phone(raw, "DE") == expected


@pytest.mark.parametrize("raw", ["", "   ", "abc", "+49", "1" * 40])
def test_normalize_phone_rejects(raw: str) -> None:
    with pytest.raises(AppError) as exc:
        normalize_phone(raw, "DE")
    assert exc.value.code == ErrorCode.INVALID_PHONE


# -------------------------------------------------------------------------------- google


@pytest.mark.anyio
async def test_dev_google_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_auth.settings, "GOOGLE_DEV_MODE", True)
    identity = await google_auth.verify_google_id_token("dev-google|42|dev@example.com|Dev User")
    assert identity.sub == "dev-42" and identity.email == "dev@example.com" and identity.name == "Dev User"

    with pytest.raises(AppError):
        await google_auth.verify_google_id_token("dev-google|missing-parts")

    monkeypatch.setattr(google_auth.settings, "GOOGLE_DEV_MODE", False)
    monkeypatch.setattr(google_auth.settings, "GOOGLE_CLIENT_IDS", [])
    with pytest.raises(AppError) as exc:
        await google_auth.verify_google_id_token("dev-google|42|dev@example.com|Dev User")
    assert exc.value.code == ErrorCode.GOOGLE_TOKEN_INVALID


@pytest.mark.anyio
async def test_google_requires_verified_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_auth.settings, "GOOGLE_CLIENT_IDS", ["web.apps"])
    claims = {"sub": "123", "email": "a@example.com", "email_verified": False}
    monkeypatch.setattr(google_auth, "_verify_with_google", lambda token: claims)
    with pytest.raises(AppError) as exc:
        await google_auth.verify_google_id_token("real-looking-token")
    assert exc.value.code == ErrorCode.GOOGLE_TOKEN_INVALID

    claims["email_verified"] = True
    identity = await google_auth.verify_google_id_token("real-looking-token")
    assert identity.sub == "123"

    def bad(token: str) -> dict:
        raise ValueError("Token used too late")

    monkeypatch.setattr(google_auth, "_verify_with_google", bad)
    with pytest.raises(AppError):
        await google_auth.verify_google_id_token("expired")
