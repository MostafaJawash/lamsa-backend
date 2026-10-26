from dataclasses import dataclass
from functools import lru_cache
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network

from fastapi import Request

from app.core.config import settings

UNKNOWN_IP = "0.0.0.0"
MAX_USER_AGENT_LENGTH = 255

IPAddress = IPv4Address | IPv6Address
IPNetwork = IPv4Network | IPv6Network


@dataclass(frozen=True)
class ClientInfo:
    ip_address: str
    user_agent: str | None


def get_client_ip(request: Request) -> str:
    """The caller's IP (login rate limits, sessions).

    X-Forwarded-For is read only when the direct peer is in TRUSTED_PROXY_IPS. Then the header is
    walked from right to left, skipping trusted addresses, and the first untrusted one is the
    client (all trusted: the leftmost). Entries that aren't IPs are ignored. Anyone else, such as
    the mobile apps calling the API directly, gets their socket address whatever they send.

    Uvicorn must run with --no-proxy-headers, otherwise it rewrites the peer from the header
    before this code sees it.
    """
    peer = _parse_ip(request.client.host) if request.client and request.client.host else None
    if peer is None:
        return UNKNOWN_IP
    networks = trusted_networks()
    if not networks:
        if settings.TRUST_PROXY_HEADERS:
            # Deprecated behaviour, kept for one release (warning logged at startup).
            first = _parse_ip(request.headers.get("x-forwarded-for", "").split(",")[0])
            if first is not None:
                return str(first)
        return str(peer)
    if not _is_trusted(peer, networks):
        return str(peer)
    entries = ",".join(request.headers.getlist("x-forwarded-for")).split(",")
    forwarded = [ip for ip in map(_parse_ip, entries) if ip is not None]
    for ip in reversed(forwarded):
        if not _is_trusted(ip, networks):
            return str(ip)
    return str(forwarded[0]) if forwarded else str(peer)


def get_client_info(request: Request) -> ClientInfo:
    user_agent = request.headers.get("user-agent")
    return ClientInfo(
        ip_address=get_client_ip(request),
        user_agent=user_agent[:MAX_USER_AGENT_LENGTH] if user_agent else None,
    )


def trusted_networks() -> tuple[IPNetwork, ...]:
    return _networks(tuple(settings.TRUSTED_PROXY_IPS))


def client_ip_policy() -> str:
    """One line for the startup log: where client IPs come from."""
    if settings.TRUSTED_PROXY_IPS:
        return f"X-Forwarded-For read only from trusted proxies {', '.join(settings.TRUSTED_PROXY_IPS)}"
    if settings.TRUST_PROXY_HEADERS:
        return "X-Forwarded-For trusted from ANY peer (deprecated TRUST_PROXY_HEADERS=true)"
    return "trusted proxies: none (socket address; X-Forwarded-For ignored)"


@lru_cache(maxsize=8)
def _networks(entries: tuple[str, ...]) -> tuple[IPNetwork, ...]:
    # Settings already rejected invalid entries.
    return tuple(ip_network(entry, strict=False) for entry in entries)


def _is_trusted(ip: IPAddress, networks: tuple[IPNetwork, ...]) -> bool:
    # A network of the other IP version never contains the address (no error).
    return any(ip in network for network in networks)


def _parse_ip(value: str) -> IPAddress | None:
    try:
        ip = ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(ip, IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip
