from dataclasses import dataclass
from ipaddress import ip_address

from fastapi import Request

from app.core.config import settings

UNKNOWN_IP = "0.0.0.0"
MAX_USER_AGENT_LENGTH = 255


@dataclass(frozen=True)
class ClientInfo:
    ip_address: str
    user_agent: str | None


def get_client_ip(request: Request) -> str:
    """The caller's IP. X-Forwarded-For is trusted only when TRUST_PROXY_HEADERS=true
    (i.e. the app runs behind our own reverse proxy); otherwise anyone could spoof it."""
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first and _is_ip(first):
            return first
    if request.client and request.client.host and _is_ip(request.client.host):
        return request.client.host
    return UNKNOWN_IP


def get_client_info(request: Request) -> ClientInfo:
    user_agent = request.headers.get("user-agent")
    return ClientInfo(
        ip_address=get_client_ip(request),
        user_agent=user_agent[:MAX_USER_AGENT_LENGTH] if user_agent else None,
    )


def _is_ip(value: str) -> bool:
    try:
        ip_address(value)
    except ValueError:
        return False
    return True
