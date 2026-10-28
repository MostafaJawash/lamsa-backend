from fastapi import APIRouter

from app.modules.admin.router import router as admin_router
from app.modules.auth.router import router as auth_router
from app.modules.banners.router import admin_router as admin_banners_router
from app.modules.banners.router import router as banners_router
from app.modules.brands.router import admin_router as admin_brands_router
from app.modules.brands.router import router as brands_router
from app.modules.cart.router import router as cart_router
from app.modules.categories.router import admin_router as admin_categories_router
from app.modules.categories.router import router as categories_router
from app.modules.delivery.router import admin_router as admin_delivery_router
from app.modules.delivery.router import router as delivery_router
from app.modules.favorites.router import router as favorites_router
from app.modules.orders.router import admin_router as admin_orders_router
from app.modules.notifications.router import admin_router as admin_notifications_router
from app.modules.notifications.router import preferences_router as notification_preferences_router
from app.modules.notifications.router import router as notifications_router
from app.modules.orders.router import router as orders_router
from app.modules.products.admin_router import router as admin_products_router
from app.modules.products.router import router as products_router
from app.modules.settings.router import admin_router as admin_settings_router
from app.modules.users.router import addresses_router, devices_router
from app.modules.users.router import router as users_router

api_router = APIRouter()

api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(notification_preferences_router)
api_router.include_router(addresses_router)
api_router.include_router(devices_router)
api_router.include_router(categories_router)
api_router.include_router(delivery_router)
api_router.include_router(brands_router)
api_router.include_router(products_router)
api_router.include_router(cart_router)
api_router.include_router(favorites_router)
api_router.include_router(orders_router)
api_router.include_router(notifications_router)
api_router.include_router(banners_router)
api_router.include_router(admin_router)
api_router.include_router(admin_categories_router)
api_router.include_router(admin_brands_router)
api_router.include_router(admin_products_router)
api_router.include_router(admin_orders_router)
api_router.include_router(admin_notifications_router)
api_router.include_router(admin_banners_router)
api_router.include_router(admin_settings_router)
api_router.include_router(admin_delivery_router)
