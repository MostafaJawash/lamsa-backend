"""Request middleware (pure ASGI, no BaseHTTPMiddleware overhead) and SQL timing.

Per request:
- X-Request-ID: the incoming one (if it looks sane) or a new one; echoed in the response and logs.
- Cache-Control: `no-store` unless the endpoint set one (cached catalog endpoints do); /media
  (development only) gets 1 year, immutable.
- Server-Timing (development only): app and db time.
- One structured log line (logger "app.access"): method, path template, status, duration, number
  and time of SQL statements, cache hits/misses. Requests slower than SLOW_REQUEST_MS and SQL
  statements slower than SLOW_QUERY_MS are logged as warnings.
"""

import contextvars
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.cache import cache_events
from app.core.config import settings

access_logger = logging.getLogger("app.access")
slow_logger = logging.getLogger("app.slow")

REQUEST_ID_HEADER = "x-request-id"
VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MEDIA_CACHE_CONTROL = "public, max-age=31536000, immutable"


@dataclass
class RequestStats:
    statements: int = 0
    db_seconds: float = 0.0


request_stats: contextvars.ContextVar[RequestStats | None] = contextvars.ContextVar("request_stats", default=None)
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)


# --------------------------------------------------------------------------- SQL timing


def _before_cursor_execute(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
    conn.info.setdefault("lamsa.query_start", []).append(time.perf_counter())


def _after_cursor_execute(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
    starts = conn.info.get("lamsa.query_start")
    if not starts:
        return
    elapsed = time.perf_counter() - starts.pop()
    stats = request_stats.get()
    if stats is not None:
        stats.statements += 1
        stats.db_seconds += elapsed
    if elapsed * 1000 >= settings.SLOW_QUERY_MS:
        slow_logger.warning(
            "Slow SQL (%.0f ms, request %s): %s", elapsed * 1000, request_id_var.get() or "-",
            " ".join(statement.split())[:500],
        )


def instrument_engine(engine: Engine) -> None:
    """Count and time every statement of this (sync) engine; idempotent."""
    if not event.contains(engine, "before_cursor_execute", _before_cursor_execute):
        event.listen(engine, "before_cursor_execute", _before_cursor_execute)
        event.listen(engine, "after_cursor_execute", _after_cursor_execute)


# ---------------------------------------------------------------------------- middleware


def path_template(path: str, route_path: str | None) -> str | None:
    """The full path template, e.g. /api/v1/products/{slug}. Routes of included routers only know
    their own part (/products/{slug}); the prefix is the leading part of the actual path (our API
    has no path parameters spanning several segments, so segment counts line up)."""
    if not route_path:
        return None
    raw = path.rstrip("/").split("/") if path != "/" else [""]
    tail = route_path.rstrip("/").split("/") if route_path != "/" else [""]
    if len(raw) < len(tail):
        return route_path
    return "/".join(raw[: len(raw) - len(tail) + 1]) + (route_path if route_path != "/" else "")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        incoming = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"x-request-id"), None)
        request_id = incoming if incoming and VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        stats = RequestStats()
        events: list[str] = []
        tokens = (request_stats.set(stats), request_id_var.set(request_id), cache_events.set(events))
        status_code = 500
        path: str = scope["path"]
        is_media = settings.APP_ENV == "development" and path.startswith("/media/")

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                if is_media:
                    headers["Cache-Control"] = MEDIA_CACHE_CONTROL
                elif "cache-control" not in headers:
                    headers["Cache-Control"] = "no-store"
                if settings.APP_ENV == "development":
                    app_ms = (time.perf_counter() - start) * 1000
                    headers["Server-Timing"] = f"app;dur={app_ms:.1f}, db;dur={stats.db_seconds * 1000:.1f}"
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            template = path_template(path, getattr(scope.get("route"), "path", None)) or ("/media/*" if is_media else path)
            record = {
                "request_id": request_id, "method": scope["method"], "path": template, "status": status_code,
                "duration_ms": round(duration_ms, 1), "db_statements": stats.statements,
                "db_ms": round(stats.db_seconds * 1000, 1),
                "cache": {"hit": events.count("hit"), "miss": events.count("miss"), "bypass": events.count("bypass")},
            }
            line = json.dumps(record, separators=(",", ":"))
            if duration_ms >= settings.SLOW_REQUEST_MS:
                slow_logger.warning("Slow request: %s", line)
            else:
                access_logger.info(line)
            for var, token in zip((request_stats, request_id_var, cache_events), tokens, strict=True):
                var.reset(token)  # type: ignore[attr-defined]
