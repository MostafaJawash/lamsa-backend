"""Keep image files consistent with the database transaction.

- Images saved during the request are deleted again if the commit fails.
- Images scheduled for deletion are removed only AFTER a successful commit (best effort:
  failures are logged, the request never fails because of cleanup).
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.integrations.storage import get_storage
from app.integrations.storage.service import ImageService, SavedImage
from app.shared.events import commit_and_publish
from app.shared.exceptions import AppError, ErrorCode, ImageTooLargeError, InvalidImageError
from app.shared.utils.uploads import file_too_large

logger = logging.getLogger(__name__)


def get_image_service() -> ImageService:
    return ImageService(get_storage(), settings.MAX_UPLOAD_BYTES)


class MediaTransaction:
    def __init__(self, session: AsyncSession, images: ImageService | None = None) -> None:
        self.session = session
        self.images = images or get_image_service()
        self._saved: list[str] = []
        self._delete_after_commit: list[str] = []

    async def save_image(self, folder: str, data: bytes) -> SavedImage:
        """Process and store an image; maps image errors to 413 FILE_TOO_LARGE / 422 INVALID_IMAGE."""
        try:
            saved = await self.images.save(folder, data)
        except ImageTooLargeError as exc:
            raise file_too_large(self.images.max_bytes) from exc
        except InvalidImageError as exc:
            raise AppError(ErrorCode.INVALID_IMAGE, status_code=422) from exc
        self._saved.append(saved.image_key)
        return saved

    def delete_after_commit(self, image_key: str | None) -> None:
        if image_key:
            self._delete_after_commit.append(image_key)

    async def commit(self) -> None:
        try:
            await commit_and_publish(self.session)
        except BaseException:
            await self.discard()
            raise
        self._saved.clear()
        for key in self._delete_after_commit:
            await self._delete_quietly(key)
        self._delete_after_commit.clear()

    async def discard(self) -> None:
        """Remove images saved during this transaction (call when the request fails)."""
        for key in self._saved:
            await self._delete_quietly(key)
        self._saved.clear()
        self._delete_after_commit.clear()

    async def _delete_quietly(self, image_key: str) -> None:
        try:
            await self.images.delete(image_key)
        except Exception:
            logger.exception("Could not delete image files for %s", image_key)
