"""Single source of "now" for database timestamps, so tests can control time.

Always call it as `clock.utcnow()` (module attribute), never `from ... import utcnow`, so a
test can monkeypatch it. JWT iat/exp use real time instead, because PyJWT validates them
against the system clock.
"""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (MySQL DATETIME columns store UTC)."""
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def now_utc() -> datetime:
    """Same clock, the name used by notification and banner code. Patch `utcnow` in tests."""
    return utcnow()
