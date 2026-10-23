"""Push backends behind one interface. firebase-admin is imported ONLY in this package.

PUSH_BACKEND=log (development, tests) logs instead of sending; PUSH_BACKEND=firebase sends
through FCM (the settings refuse to start without a readable FIREBASE_CREDENTIALS_PATH).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

import anyio

logger = logging.getLogger(__name__)

FCM_BATCH_SIZE = 500

# Result codes (strings, so callers never import firebase-admin).
OK = "OK"
UNREGISTERED = "UNREGISTERED"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
SENDER_ID_MISMATCH = "SENDER_ID_MISMATCH"
UNAVAILABLE = "UNAVAILABLE"
INTERNAL = "INTERNAL"
OTHER = "OTHER"
INVALID_TOKEN_CODES = frozenset({UNREGISTERED, INVALID_ARGUMENT, SENDER_ID_MISMATCH})
RETRYABLE_CODES = frozenset({UNAVAILABLE, INTERNAL})


@dataclass(frozen=True)
class PushMessage:
    token: str
    title: str
    body: str
    data: dict[str, str] = field(default_factory=dict)  # FCM data values must be strings
    channel: str = "marketing"  # Android notification channel id
    badge: int | None = None  # iOS badge (unread count) for per-user messages


@dataclass(frozen=True)
class PushResult:
    token: str
    code: str  # OK or an error code above
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.code == OK

    @property
    def invalid_token(self) -> bool:
        return self.code in INVALID_TOKEN_CODES

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


class PushSender(Protocol):
    async def send(self, messages: Sequence[PushMessage]) -> list[PushResult]:
        """Send each message (up to any number; implementations batch by 500)."""
        ...


class LogPushSender:
    """Development/tests without credentials: log every push, report success."""

    async def send(self, messages: Sequence[PushMessage]) -> list[PushResult]:
        for m in messages:
            logger.info("PUSH (log backend) token=%s… title=%r data=%s", m.token[:12], m.title, m.data)
        return [PushResult(m.token, OK) for m in messages]


class FirebasePushSender:
    def __init__(self, credentials_path: str) -> None:
        self.credentials_path = credentials_path
        self._app: Any = None

    def _ensure_app(self) -> Any:
        if self._app is None:
            import firebase_admin
            from firebase_admin import credentials

            self._app = firebase_admin.initialize_app(credentials.Certificate(self.credentials_path), name="lamsa-push")
        return self._app

    async def send(self, messages: Sequence[PushMessage]) -> list[PushResult]:
        results: list[PushResult] = []
        for start in range(0, len(messages), FCM_BATCH_SIZE):
            chunk = list(messages[start : start + FCM_BATCH_SIZE])
            # The SDK is blocking: run it in a worker thread.
            results.extend(await anyio.to_thread.run_sync(self._send_chunk, chunk))
        return results

    def _send_chunk(self, chunk: list[PushMessage]) -> list[PushResult]:
        from firebase_admin import messaging

        app = self._ensure_app()
        fcm_messages = [
            messaging.Message(
                token=m.token,
                notification=messaging.Notification(title=m.title, body=m.body),
                data=m.data,
                android=messaging.AndroidConfig(notification=messaging.AndroidNotification(channel_id=m.channel)),
                apns=messaging.APNSConfig(payload=messaging.APNSPayload(aps=messaging.Aps(sound="default", badge=m.badge))),
            )
            for m in chunk
        ]
        response = messaging.send_each(fcm_messages, app=app)
        return [_result(m.token, r) for m, r in zip(chunk, response.responses, strict=True)]


def _result(token: str, response: Any) -> PushResult:
    if response.success:
        return PushResult(token, OK)
    return PushResult(token, error_code(response.exception), str(response.exception))


def error_code(exc: BaseException | None) -> str:
    """Map firebase-admin exceptions to our codes."""
    from firebase_admin import exceptions, messaging

    if isinstance(exc, messaging.UnregisteredError):
        return UNREGISTERED
    if isinstance(exc, messaging.SenderIdMismatchError):
        return SENDER_ID_MISMATCH
    if isinstance(exc, exceptions.InvalidArgumentError):
        return INVALID_ARGUMENT
    if isinstance(exc, exceptions.UnavailableError):
        return UNAVAILABLE
    if isinstance(exc, exceptions.InternalError):
        return INTERNAL
    return OTHER


_override: PushSender | None = None


def set_push_sender(sender: PushSender | None) -> None:
    """Tests: install a capturing fake (None restores the configured backend)."""
    global _override
    _override = sender


@lru_cache
def _configured_sender() -> PushSender:
    from app.core.config import settings

    if settings.PUSH_BACKEND == "firebase":
        return FirebasePushSender(str(settings.FIREBASE_CREDENTIALS_PATH))
    return LogPushSender()


def get_push_sender() -> PushSender:
    return _override or _configured_sender()
