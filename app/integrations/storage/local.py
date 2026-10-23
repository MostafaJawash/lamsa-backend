import os
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

import anyio

from app.shared.exceptions import InvalidStorageKeyError


class LocalStorage:
    """Stores media as files under a root folder.

    In development FastAPI serves the folder at /media; in production Nginx/Caddy serves it
    directly. content_type is not stored: the web server derives it from the file extension.
    """

    def __init__(self, root: Path, base_url: str) -> None:
        self.root = Path(root).resolve()
        self.base_url = base_url.rstrip("/")

    @staticmethod
    def check_key(key: str) -> None:
        """Reject keys that could escape the root: syntactic only, no filesystem access."""
        if (
            not key
            or ".." in key
            or "\\" in key
            or PurePosixPath(key).is_absolute()
            or PureWindowsPath(key).is_absolute()
            or PureWindowsPath(key).drive
        ):
            raise InvalidStorageKeyError(f"Invalid storage key: {key!r}")

    def path_for(self, key: str) -> Path:
        """Map a key to a path under root, rejecting anything that could escape it (including
        through symlinks: the real path is resolved, so use this for file operations only)."""
        self.check_key(key)
        path = (self.root / key).resolve()
        if path == self.root or not path.is_relative_to(self.root):
            raise InvalidStorageKeyError(f"Storage key resolves outside MEDIA_ROOT: {key!r}")
        return path

    async def save(self, key: str, data: bytes, content_type: str) -> None:
        path = self.path_for(key)
        await anyio.to_thread.run_sync(self._write_atomic, path, data)

    async def delete(self, keys: list[str]) -> None:
        paths = [self.path_for(key) for key in keys]
        await anyio.to_thread.run_sync(self._unlink_all, paths)

    async def exists(self, key: str) -> bool:
        path = self.path_for(key)
        return await anyio.to_thread.run_sync(path.is_file)

    async def check(self) -> bool:
        return await anyio.to_thread.run_sync(self._is_writable)

    def url(self, key: str) -> str:
        # Built for every image in every response: no filesystem access here (resolving the
        # real path cost ~1 ms per URL on Windows and dominated cached catalog responses).
        self.check_key(key)
        return f"{self.base_url}/{key}"

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        # Write to a temp file in the same folder, then rename over the target, so readers
        # never see a half-written file.
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(data)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    @staticmethod
    def _unlink_all(paths: list[Path]) -> None:
        for path in paths:
            path.unlink(missing_ok=True)

    def _is_writable(self) -> bool:
        # os.access() is unreliable for folders on Windows, so probe with a real file.
        if not self.root.is_dir():
            return False
        try:
            with tempfile.TemporaryFile(dir=self.root):
                pass
        except OSError:
            return False
        return True
