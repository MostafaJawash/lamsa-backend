import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.errors import register_exception_handlers
from app.shared.exceptions import ErrorCode, NotFound, TooManyRequests
from conftest import error_code

pytestmark = pytest.mark.anyio


async def test_unknown_route_is_404_in_error_format(client: AsyncClient) -> None:
    response = await client.get("/api/v1/nope")
    assert response.status_code == 404
    assert error_code(response) == "NOT_FOUND"
    assert response.json()["error"]["message"]


async def test_wrong_method_is_405(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/login")
    assert response.status_code == 405
    assert error_code(response) == "METHOD_NOT_ALLOWED"


async def test_validation_error_format(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/register", json={"name": "", "password": "x"})
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert body["message"] == "البيانات المرسلة غير صحيحة."
    fields = {d["field"] for d in body["details"]}
    assert {"name", "phone"} <= fields
    assert all(set(d) == {"field", "message"} for d in body["details"])


async def test_malformed_json(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert error_code(response) == "VALIDATION_ERROR"


async def test_unhandled_error_is_generic_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret database password in message")

    @app.get("/missing")
    async def missing() -> None:
        raise NotFound(ErrorCode.ADDRESS_NOT_FOUND)

    @app.get("/slow-down")
    async def slow_down() -> None:
        raise TooManyRequests(ErrorCode.TOO_MANY_ATTEMPTS, retry_after=42)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        with caplog.at_level(logging.ERROR, logger="app.errors"):
            response = await c.get("/boom")
        assert response.status_code == 500
        assert error_code(response) == "INTERNAL_ERROR"
        assert "secret" not in response.text and "RuntimeError" not in response.text
        assert any("Unhandled error" in r.message and r.exc_info for r in caplog.records)

        missing_response = await c.get("/missing")
        assert missing_response.status_code == 404 and error_code(missing_response) == "ADDRESS_NOT_FOUND"

        limited = await c.get("/slow-down")
        assert limited.status_code == 429 and limited.headers["Retry-After"] == "42"
        assert limited.json()["error"]["details"] == {"retry_after": 42}
