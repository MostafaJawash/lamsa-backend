"""Retry a write transaction after an InnoDB deadlock.

MySQL picks a victim when two transactions lock rows in opposite orders (error 1213) and asks the
loser to start again. Cart writes and checkout have their own retry loop (`CartService.retrying`);
this is the same rule for the other services: up to DEADLOCK_ATTEMPTS tries in all with a short
random backoff, and a deadlock that persists is overload, so 503 SERVICE_BUSY, never a 500.

The operation must read everything it needs from plain values it captured BEFORE the first
attempt (ids, not ORM objects): `session.rollback()` expires every loaded object, and touching an
expired attribute inside an async session raises instead of reloading.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared.exceptions import ServiceBusy

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEADLOCK = 1213
DEADLOCK_ATTEMPTS = 3  # in all, for one write transaction


def is_deadlock(exc: BaseException) -> bool:
    return isinstance(exc, OperationalError) and getattr(exc.orig, "args", [None])[0] == DEADLOCK


async def retry_on_deadlock(session: AsyncSession, operation: Callable[[], Awaitable[T]], *, what: str) -> T:
    """Run `operation` (one whole transaction, committing at its end). On a deadlock: roll back,
    wait a little and run it again, up to DEADLOCK_ATTEMPTS in all, then raise ServiceBusy."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return await operation()
        except OperationalError as exc:
            if not is_deadlock(exc):
                raise
            await session.rollback()
            if attempt >= DEADLOCK_ATTEMPTS:
                logger.warning("%s deadlocked %d times: SERVICE_BUSY", what, attempt)
                raise ServiceBusy(retry_after=1) from exc
            logger.info("Retrying %s after a deadlock (attempt %d)", what, attempt)
            await asyncio.sleep(random.uniform(0.01, 0.05) * attempt)
