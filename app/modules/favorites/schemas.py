from pydantic import BaseModel, Field


class FavoriteIds(BaseModel):
    product_ids: list[int] = Field(description="Visible favorites, newest first")


class FavoritesMerge(BaseModel):
    product_ids: list[int] = Field(max_length=200)


class SkippedFavorite(BaseModel):
    product_id: int
    reason: str


class FavoritesMergeOut(BaseModel):
    added: list[int]
    skipped: list[SkippedFavorite]


class MoveToCart(BaseModel):
    variant_id: int | None = Field(default=None, description="Default: first active variant in stock")
    quantity: int = Field(default=1, ge=1, le=1000)
