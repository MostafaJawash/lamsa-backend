from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.modules.notifications.schemas import LinkIn, LinkOut, UtcDateTime
from app.shared.schemas import ImageUrls


class BannerState(StrEnum):
    """Computed from is_active and the time window."""

    inactive = "inactive"
    scheduled = "scheduled"  # starts in the future
    active = "active"  # shown in GET /banners (if its link target is visible)
    expired = "expired"


class BannerOut(BaseModel):
    id: int
    title: str
    subtitle: str | None
    image: ImageUrls | None
    link: LinkOut


class AdminBannerOut(BannerOut):
    sort_order: int
    is_active: bool
    starts_at: datetime | None
    ends_at: datetime | None
    state: BannerState
    created_at: datetime
    updated_at: datetime


class BannerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=150)
    subtitle: str | None = Field(default=None, max_length=255)
    link: LinkIn | None = None
    is_active: bool | None = None
    starts_at: UtcDateTime | None = None  # null clears it
    ends_at: UtcDateTime | None = None  # null clears it


class BannerOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    banner_ids: list[int] = Field(description="Every banner exactly once; the first is shown first")
