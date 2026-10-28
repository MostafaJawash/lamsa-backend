"""Import every model so Base.metadata is complete (used by Alembic autogenerate)."""

from app.db.base import Base
from app.modules.auth.models import LoginAttempt, RefreshToken
from app.modules.banners.models import Banner
from app.modules.brands.models import Brand
from app.modules.cart.models import CartItem
from app.modules.categories.models import Category, CategoryAttribute, CategoryVariantOption
from app.modules.delivery.models import DeliveryMethod
from app.modules.favorites.models import Favorite
from app.modules.notifications.models import Notification, NotificationRead, UserAlertLog
from app.modules.orders.models import Order, OrderItem, OrderStatusHistory, OrderTotal
from app.modules.products.models import BundleItem, Product, ProductImage, ProductVariant
from app.modules.settings.models import StoreSetting
from app.modules.users.models import User, UserAddress, UserDevice

__all__ = [
    "Base",
    "Banner",
    "Brand",
    "BundleItem",
    "CartItem",
    "Category",
    "CategoryAttribute",
    "CategoryVariantOption",
    "DeliveryMethod",
    "Favorite",
    "LoginAttempt",
    "Notification",
    "NotificationRead",
    "Order",
    "OrderItem",
    "OrderStatusHistory",
    "OrderTotal",
    "Product",
    "ProductImage",
    "ProductVariant",
    "RefreshToken",
    "StoreSetting",
    "User",
    "UserAddress",
    "UserAlertLog",
    "UserDevice",
]
