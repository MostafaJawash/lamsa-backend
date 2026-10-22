"""Prepare media storage: create MEDIA_ROOT if missing and check it is writable.

Run with:  python -m scripts.init_storage
"""

import asyncio
import sys

from app.core.config import settings
from app.integrations.storage import get_storage


async def main() -> int:
    root = settings.MEDIA_ROOT.resolve()
    created = not root.exists()
    root.mkdir(parents=True, exist_ok=True)

    ok = await get_storage().check()
    print(f"MEDIA_ROOT {root} ({'created' if created else 'exists'}), writable: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
