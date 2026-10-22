from fastapi import APIRouter

from app.modules.admin.router import router as admin_router
from app.modules.auth.router import router as auth_router
from app.modules.users.router import addresses_router, devices_router
from app.modules.users.router import router as users_router

api_router = APIRouter()

api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(addresses_router)
api_router.include_router(devices_router)
api_router.include_router(admin_router)
