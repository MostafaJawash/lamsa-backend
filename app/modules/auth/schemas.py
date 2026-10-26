from typing import Literal

from pydantic import BaseModel, Field

from app.modules.users.schemas import DeviceName, Name, Password, PhoneInput, UserOut


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds")
    refresh_expires_in: int = Field(description="Refresh token lifetime in seconds")
    user: UserOut


class RegisterRequest(BaseModel):
    name: Name
    phone: PhoneInput
    password: Password
    device_name: DeviceName | None = None


class LoginRequest(BaseModel):
    phone: PhoneInput
    password: Password
    device_name: DeviceName | None = None


class GoogleLoginRequest(BaseModel):
    id_token: str = Field(min_length=1, max_length=8192)
    device_name: DeviceName | None = None


class GoogleProfile(BaseModel):
    email: str | None
    name: str | None


class GoogleLoginResponse(BaseModel):
    """registered=true: tokens is set. registered=false: show the name + phone screen
    (prefilled with profile.name) and call /auth/google/complete with signup_token."""

    registered: bool
    tokens: TokenPair | None = None
    signup_token: str | None = None
    profile: GoogleProfile | None = None


class GoogleCompleteRequest(BaseModel):
    signup_token: str = Field(min_length=1, max_length=4096)
    name: Name
    phone: PhoneInput
    device_name: DeviceName | None = None


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class PasswordChangeRequest(BaseModel):
    current_password: Password
    new_password: Password
