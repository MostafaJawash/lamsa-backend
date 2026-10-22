import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.db.session import engine
from app.integrations.storage import get_storage

logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.GOOGLE_DEV_MODE:
        logger.warning(
            "GOOGLE_DEV_MODE is ON: tokens like 'dev-google|<sub>|<email>|<name>' are accepted "
            "without Google. Never enable this outside local development."
        )
    yield
    await engine.dispose()


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

register_exception_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,  # auth uses the Authorization header, not cookies
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)

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
