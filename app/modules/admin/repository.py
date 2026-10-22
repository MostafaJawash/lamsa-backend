"""Admin queries live in app.modules.users.repository (list_users, count_orders); this module
re-exports them so the admin module keeps the router/service/repository layout."""

from app.modules.users.repository import count_orders, get_user, list_users

__all__ = ["count_orders", "get_user", "list_users"]
