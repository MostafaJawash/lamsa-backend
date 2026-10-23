"""Multipart upload helpers.

Starlette parses the whole multipart body before the endpoint runs, so reading a file in
chunks alone cannot stop a huge upload from being received. Upload routes therefore also use
`limit_upload_body`, which rejects requests whose Content-Length is clearly too big.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, UploadFile, status

from app.core.config import settings
from app.shared.exceptions import AppError, ErrorCode

CHUNK_SIZE = 64 * 1024
MULTIPART_OVERHEAD = 1024 * 1024


def file_too_large(max_bytes: int) -> AppError:
    return AppError(
        ErrorCode.FILE_TOO_LARGE,
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        details={"max_bytes": max_bytes},
    )


async def read_upload(upload: UploadFile, max_bytes: int | None = None) -> bytes:
    """Read an uploaded file in chunks, stopping with 413 FILE_TOO_LARGE past max_bytes."""
    limit = max_bytes if max_bytes is not None else settings.MAX_UPLOAD_BYTES
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(CHUNK_SIZE):
        total += len(chunk)
        if total > limit:
            raise file_too_large(limit)
        chunks.append(chunk)
    return b"".join(chunks)


def limit_upload_body(max_files: int) -> Callable[[Request], Coroutine[Any, Any, None]]:
    """Dependency: 413 when Content-Length exceeds max_files * MAX_UPLOAD_MB + 1 MB."""

    async def dependency(request: Request) -> None:
        length = request.headers.get("content-length")
        limit = max_files * settings.MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD
        if length is not None and length.isdigit() and int(length) > limit:
            raise file_too_large(settings.MAX_UPLOAD_BYTES)

    return dependency
