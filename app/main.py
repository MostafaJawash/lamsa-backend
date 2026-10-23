import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.v1.router import api_router
from app.core.cache import start_cache, stop_cache
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.middleware import RequestContextMiddleware, instrument_engine
from app.db.session import engine
from app.integrations.storage import get_storage
from app.modules.notifications import handlers as notification_handlers
from app.shared import catalog_cache
from app.workers.runner import Worker

logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.GOOGLE_DEV_MODE:
        logger.warning(
            "GOOGLE_DEV_MODE is ON: tokens like 'dev-google|<sub>|<email>|<name>' are accepted "
            "without Google. Never enable this outside local development."
        )
    # Connect the cache (best effort) and bump the catalog version: a fresh deploy never serves
    # entries written by the previous code.
    await start_cache()
    worker: Worker | None = None
    worker_task: asyncio.Task[None] | None = None
    if settings.run_worker_in_app:
        worker = Worker()
        worker_task = asyncio.create_task(worker.run())
        logger.info("Background worker runs inside the API process (RUN_WORKER_IN_APP)")
    yield
    if worker is not None and worker_task is not None:
        worker.stop()
        await worker_task
    await stop_cache()
    await engine.dispose()


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

# Domain events -> notification rows, and catalog cache invalidation (after each commit).
notification_handlers.register()
catalog_cache.register()
instrument_engine(engine.sync_engine)

register_exception_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,  # auth uses the Authorization header, not cookies
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After", "ETag", "X-Request-ID"],
)
# Outermost: request id, Cache-Control default, timing and the access log for every request.
app.add_middleware(RequestContextMiddleware)

app.include_router(api_router, prefix="/api/v1")

# Development only. In production Nginx/Caddy serves MEDIA_ROOT at /media directly, with
# long cache headers (Cache-Control: public, max-age=31536000, immutable); see README.
if settings.APP_ENV == "development":
    settings.MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=settings.MEDIA_ROOT), name="media")


async def _database_ok() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


async def _storage_ok() -> bool:
    try:
        return await get_storage().check()
    except Exception:
        return False


@app.get("/health", tags=["system"])
async def health():
    database_ok = await _database_ok()
    storage_ok = await _storage_ok()
    body = {
        "status": "ok" if database_ok and storage_ok else "error",
        "database": "ok" if database_ok else "unreachable",
        "storage": "ok" if storage_ok else "unavailable",
    }
    if database_ok and storage_ok:
        return body
    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=body)
