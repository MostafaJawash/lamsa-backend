from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, StringConstraints, field_validator

from app.modules.users.enums import AuthProvider, DevicePlatform, UserRole

MAX_EMAIL_LENGTH = 255

# Reusable request field types. Strings are trimmed; lengths match the database columns.
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
PhoneInput = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
# Rules (8..128, not all digits, not the phone) are checked in the service with Arabic messages;
# the hard cap here only stops absurdly large inputs reaching Argon2.
Password = Annotated[str, StringConstraints(min_length=1, max_length=1024)]
DeviceName = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]


def _blank_to_none(value: str | None) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    phone: str
    email: str | None
    role: UserRole
    auth_provider: AuthProvider
    must_change_password: bool
    created_at: datetime


class UserUpdate(BaseModel):
    name: Name | None = None
    email: EmailStr | None = None

    @field_validator("email", mode="before")
    @classmethod
    def _email_blank(cls, value: str | None) -> str | None:
        return _blank_to_none(value)

    @field_validator("email", mode="after")
    @classmethod
    def _email_length(cls, value: str | None) -> str | None:
        if value is not None and len(value) > MAX_EMAIL_LENGTH:
            raise ValueError(f"email must be at most {MAX_EMAIL_LENGTH} characters")
        return value


class PhoneChange(BaseModel):
    phone: PhoneInput
    current_password: Password | None = None


class AccountDelete(BaseModel):
    current_password: Password | None = None


# ----------------------------------------------------------------------------- addresses

AddressLabel = Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)]
AddressCity = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
AddressStreet = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
AddressBuilding = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]
AddressNotes = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


class AddressCreate(BaseModel):
    label: AddressLabel | None = None
    city: AddressCity
    street: AddressStreet
    building: AddressBuilding | None = None
    notes: AddressNotes | None = None
    is_default: bool = False

    @field_validator("label", "building", "notes", mode="after")
    @classmethod
    def _optional_blank(cls, value: str | None) -> str | None:
        return _blank_to_none(value)


class AddressUpdate(BaseModel):
    """Only sent fields change. is_default=true makes it the default; to move the default
    elsewhere set it on another address (the default cannot simply be unset)."""

    label: AddressLabel | None = None
    city: AddressCity | None = None
    street: AddressStreet | None = None
    building: AddressBuilding | None = None
    notes: AddressNotes | None = None
    is_default: bool | None = None

    @field_validator("label", "building", "notes", mode="after")
    @classmethod
    def _optional_blank(cls, value: str | None) -> str | None:
        return _blank_to_none(value)


class AddressOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str | None
    city: str
    street: str
    building: str | None
    notes: str | None
    is_default: bool
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------------------------- devices

FcmToken = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]


class DeviceRegister(BaseModel):
    fcm_token: FcmToken
    platform: DevicePlatform


class DeviceUnregister(BaseModel):
    fcm_token: FcmToken
