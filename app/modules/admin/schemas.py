from datetime import datetime

from pydantic import BaseModel

from app.modules.users.enums import UserRole
from app.modules.users.schemas import UserOut


class AdminUserOut(UserOut):
    is_active: bool
    last_login_at: datetime | None
    orders_count: int


class AdminUserUpdate(BaseModel):
    is_active: bool | None = None
    role: UserRole | None = None


class TemporaryPasswordOut(BaseModel):
    """Shown once. Tell it to the customer; they must change it at next login."""

    temporary_password: str
