from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.shared.schemas import ImageUrls

BrandName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Slug = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class BrandRef(BaseModel):
    id: int
    name: str
    slug: str


class BrandOut(BaseModel):
    id: int
    name: str
    slug: str
    description: str | None
    logo: ImageUrls | None
    is_own_brand: bool
    product_count: int


class AdminBrandOut(BrandOut):
    is_active: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime


class BrandCreate(BaseModel):
    name: BrandName
    slug: Slug | None = None
    description: str | None = Field(default=None, max_length=5000)
    is_own_brand: bool = False
    is_active: bool = True
    sort_order: int = Field(default=0, ge=-1_000_000, le=1_000_000)


class BrandUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: BrandName | None = None
    slug: Slug | None = None
    description: str | None = Field(default=None, max_length=5000)
    is_own_brand: bool | None = None
    is_active: bool | None = None
    sort_order: int | None = Field(default=None, ge=-1_000_000, le=1_000_000)
