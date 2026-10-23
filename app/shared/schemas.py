from pydantic import BaseModel

from app.integrations.storage.images import ImageSize
from app.integrations.storage.transaction import get_image_service


class ImageUrls(BaseModel):
    thumb: str
    medium: str
    large: str


def image_urls(image_key: str | None) -> ImageUrls | None:
    """Public URLs of all sizes for an image key (no I/O)."""
    if not image_key:
        return None
    service = get_image_service()
    return ImageUrls(**{size.value: service.url(image_key, size) for size in ImageSize})
