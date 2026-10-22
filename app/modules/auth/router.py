from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.modules.auth.schemas import (
    GoogleCompleteRequest,
    GoogleLoginRequest,
    GoogleLoginResponse,
    LoginRequest,
    LogoutRequest,
    PasswordChangeRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
)
from app.modules.auth.service import AuthService
from app.shared.dependencies.auth import (
    AuthContextAllowPasswordChange,
    BearerCredentials,
    CurrentUser,
    DbSession,
)
from app.shared.dependencies.request import get_client_info

router = APIRouter(prefix="/auth", tags=["auth"])


def get_auth_service(request: Request, session: DbSession) -> AuthService:
    return AuthService(session, get_client_info(request))


Service = Annotated[AuthService, Depends(get_auth_service)]


@router.post("/register", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
async def register(data: RegisterRequest, service: Service) -> TokenPair:
    """Create a phone + password account."""
    return await service.register(data)


@router.post("/login", response_model=TokenPair)
async def login(data: LoginRequest, service: Service) -> TokenPair:
    """Log in with phone + password. Wrong phone or password: 401 INVALID_CREDENTIALS."""
    return await service.login(data)


@router.post("/google", response_model=GoogleLoginResponse)
async def google_login(data: GoogleLoginRequest, service: Service) -> GoogleLoginResponse:
    """Sign in with a Google ID token. New users get a signup_token for /auth/google/complete."""
    return await service.google_login(data)


@router.post("/google/complete", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
async def google_complete(data: GoogleCompleteRequest, service: Service) -> TokenPair:
    """Finish a Google signup with name + phone."""
    return await service.google_complete(data)


@router.post("/refresh", response_model=TokenPair)
async def refresh(data: RefreshRequest, service: Service) -> TokenPair:
    """Exchange a refresh token for a new pair. The old refresh token stops working.
    Send only one refresh request at a time."""
    return await service.refresh(data.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(data: LogoutRequest, service: Service, credentials: BearerCredentials) -> Response:
    """End this session (the refresh token's family). The access token is optional; if sent,
    it must belong to the same user. Always 204."""
    access_token = credentials.credentials if credentials else None
    await service.logout(data.refresh_token, access_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(user: CurrentUser, service: Service) -> Response:
    """End all sessions of the current user."""
    await service.logout_all(user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/password/change", response_model=TokenPair)
async def change_password(
    data: PasswordChangeRequest, context: AuthContextAllowPasswordChange, service: Service
) -> TokenPair:
    """Change the password (phone accounts). Ends all other sessions and returns a new pair."""
    return await service.change_password(context, data)
