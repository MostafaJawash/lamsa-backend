from typing import Annotated

from fastapi import APIRouter, Body, Depends, Response, status

from app.modules.users.models import User, UserAddress
from app.modules.users.schemas import (
    AccountDelete,
    AddressCreate,
    AddressOut,
    AddressUpdate,
    DeviceRegister,
    DeviceUnregister,
    PhoneChange,
    UserOut,
    UserUpdate,
)
from app.modules.users.service import AddressService, DeviceService, UserService
from app.shared.dependencies.auth import CurrentUser, CurrentUserAllowPasswordChange, DbSession

router = APIRouter(prefix="/users/me", tags=["users"])
addresses_router = APIRouter(prefix="/users/me/addresses", tags=["addresses"])
devices_router = APIRouter(prefix="/users/me/devices", tags=["devices"])


def get_user_service(session: DbSession) -> UserService:
    return UserService(session)


def get_address_service(session: DbSession) -> AddressService:
    return AddressService(session)


def get_device_service(session: DbSession) -> DeviceService:
    return DeviceService(session)


Users = Annotated[UserService, Depends(get_user_service)]
Addresses = Annotated[AddressService, Depends(get_address_service)]
Devices = Annotated[DeviceService, Depends(get_device_service)]

# ----------------------------------------------------------------------------------- me


@router.get("", response_model=UserOut)
async def get_me(user: CurrentUserAllowPasswordChange) -> User:
    return user


@router.patch("", response_model=UserOut)
async def update_me(data: UserUpdate, user: CurrentUser, service: Users) -> User:
    return await service.update_profile(user, data)


@router.post("/phone", response_model=UserOut)
async def change_phone(data: PhoneChange, user: CurrentUser, service: Users) -> User:
    """Phone accounts must send current_password (the phone is their login)."""
    return await service.change_phone(user, data)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(
    user: CurrentUser,
    service: Users,
    data: Annotated[AccountDelete | None, Body()] = None,
) -> Response:
    """Delete the account (phone accounts confirm with current_password). Accounts with orders
    are anonymised instead, so order history stays intact."""
    await service.delete_account(user, data.current_password if data else None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------------- addresses


@addresses_router.get("", response_model=list[AddressOut])
async def list_addresses(user: CurrentUser, service: Addresses) -> list[UserAddress]:
    return await service.list(user)


@addresses_router.post("", response_model=AddressOut, status_code=status.HTTP_201_CREATED)
async def create_address(data: AddressCreate, user: CurrentUser, service: Addresses) -> UserAddress:
    return await service.create(user, data)


@addresses_router.patch("/{address_id}", response_model=AddressOut)
async def update_address(address_id: int, data: AddressUpdate, user: CurrentUser, service: Addresses) -> UserAddress:
    return await service.update(user, address_id, data)


@addresses_router.delete("/{address_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_address(address_id: int, user: CurrentUser, service: Addresses) -> Response:
    await service.delete(user, address_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@addresses_router.post("/{address_id}/default", response_model=AddressOut)
async def set_default_address(address_id: int, user: CurrentUser, service: Addresses) -> UserAddress:
    return await service.set_default(user, address_id)


# ------------------------------------------------------------------------------- devices


@devices_router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def register_device(data: DeviceRegister, user: CurrentUser, service: Devices) -> Response:
    """Register or refresh this device's FCM token for push notifications."""
    await service.register(user, data)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@devices_router.post("/unregister", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_device(data: DeviceUnregister, user: CurrentUser, service: Devices) -> Response:
    """Remove this device's FCM token. Call it before logout. Always 204."""
    await service.unregister(user, data.fcm_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
