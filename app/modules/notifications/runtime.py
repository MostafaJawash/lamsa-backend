"""Runtime hooks of the notification pipeline, injectable for tests.

- notification_session(): the session handlers, the dispatcher and the jobs use. Never the
  request's session: handlers run after the request committed, in their own transaction.
- kick_dispatch(): send what is due right away, in a background task (after the request's
  commit, outside any request transaction). The worker's 30 s loop is the safety net.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@asynccontextmanager
async def _default_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


_session_factory: SessionFactory = _default_session
_immediate_dispatch = True
_tasks: set[asyncio.Task[None]] = set()


def notification_session() -> AbstractAsyncContextManager[AsyncSession]:
    return _session_factory()


def set_session_factory(factory: SessionFactory | None) -> None:
    """Tests: bind the pipeline to the test transaction (None restores the default)."""
    global _session_factory
    _session_factory = factory or _default_session


def set_immediate_dispatch(enabled: bool) -> None:
    """Tests switch the background dispatch off and call the dispatcher themselves."""
    global _immediate_dispatch
    _immediate_dispatch = enabled


def kick_dispatch() -> None:
    if not _immediate_dispatch:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    from app.modules.notifications.dispatcher import dispatch_due

    async def run() -> None:
        try:
            await dispatch_due()
        except Exception:
            logger.exception("Immediate notification dispatch failed (the worker will retry)")

    task = loop.create_task(run())
    _tasks.add(task)  # keep a reference until done
    task.add_done_callback(_tasks.discard)
