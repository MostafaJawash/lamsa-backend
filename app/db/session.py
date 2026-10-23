from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401  (register every model so relationships resolve at first use)
from app.core.config import settings

# Every connection works in UTC so CURRENT_TIMESTAMP defaults are stored as UTC.
CONNECT_ARGS: dict[str, Any] = {"init_command": "SET time_zone = '+00:00'"}

# A full pool waits DB_POOL_TIMEOUT seconds, then raises sqlalchemy.exc.TimeoutError, which the
# error handlers turn into 503 SERVICE_BUSY with Retry-After (never a 500).
engine = create_async_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_recycle=settings.DB_POOL_RECYCLE,
    echo=settings.DEBUG,
    connect_args=CONNECT_ARGS,
)

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def release_read_transaction(session: AsyncSession | None) -> None:
    """End a READ-ONLY transaction so its pooled connection is free while the request does
    something else (waits for password hashing, the cache, or the event loop). Loaded objects stay
    usable (expire_on_commit=False); the next query starts a new transaction. Does nothing when
    changes are pending: those must stay in one transaction."""
    if session is not None and session.in_transaction() and not (session.new or session.dirty or session.deleted):
        await session.commit()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
