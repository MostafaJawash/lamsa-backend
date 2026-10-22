import logging
from dataclasses import dataclass
from uuid import uuid4

import anyio

from app.integrations.storage.base import StorageBackend
from app.integrations.storage.images import ImageSize, process_image

logger = logging.getLogger(__name__)

WEBP_CONTENT_TYPE = "image/webp"


@dataclass(frozen=True)
class SavedImage:
    image_key: str  # base key without size suffix, e.g. products/42/3f9c...e1
    width: int  # of the large size
    height: int


class ImageService:
    """Processes uploads into WebP sizes and stores them as f"{image_key}_{size}.webp"."""

    def __init__(self, storage: StorageBackend, max_bytes: int) -> None:
        self.storage = storage
        self.max_bytes = max_bytes

    async def save(self, folder: str, data: bytes) -> SavedImage:
        """Store all sizes under a new key in folder (e.g. "products/42", "banners").

        If any size fails to store, the sizes already stored are deleted and the error is
        re-raised, so no partial image is left behind.
        """
        processed = await anyio.to_thread.run_sync(process_image, data, self.max_bytes)
        image_key = f"{folder.strip('/')}/{uuid4().hex}"

        stored: list[str] = []
        try:
            for size, content in processed.variants.items():
                key = self.key_for(image_key, size)
                await self.storage.save(key, content, WEBP_CONTENT_TYPE)
                stored.append(key)
        except BaseException:
            if stored:
                with anyio.CancelScope(shield=True):
                    try:
                        await self.storage.delete(stored)
                    except Exception:
                        logger.exception("Could not clean up partially saved image %s", image_key)
            raise

        return SavedImage(image_key=image_key, width=processed.width, height=processed.height)

    async def delete(self, image_key: str) -> None:
        await self.storage.delete([self.key_for(image_key, size) for size in ImageSize])

    def url(self, image_key: str, size: ImageSize = ImageSize.medium) -> str:
        return self.storage.url(self.key_for(image_key, size))

    def urls(self, image_key: str) -> dict[str, str]:
        return {size.value: self.url(image_key, size) for size in ImageSize}

    @staticmethod
    def key_for(image_key: str, size: ImageSize) -> str:
        return f"{image_key}_{size.value}.webp"
