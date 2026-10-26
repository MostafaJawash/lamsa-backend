from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app.core.config import Settings, settings
from app.shared.dependencies.request import UNKNOWN_IP, client_ip_policy, get_client_ip

NGINX = "10.0.0.5"
WEB = "10.0.0.6"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"DB_NAME": "x_test", "DB_USER": "u", "DB_PASSWORD": "p", "JWT_SECRET": "s" * 48}
    return Settings(_env_file=None, **values, **overrides)  # type: ignore[call-arg]


def request_from(peer: str | None, *forwarded: str) -> Request:
    headers = [(b"x-forwarded-for", value.encode()) for value in forwarded]
    scope = {
        "type": "http", "method": "GET", "path": "/", "headers": headers,
        "client": (peer, 50000) if peer is not None else None,
    }
    return Request(scope)


@pytest.fixture
def trusted(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", [NGINX, WEB, "fd00:1::/64"])
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", False)
    yield


# ------------------------------------------------------------------------- no trusted proxies


def test_default_trusts_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", [])
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", False)
    assert get_client_ip(request_from("203.0.113.9", "198.51.100.1")) == "203.0.113.9"
    assert "none" in client_ip_policy()


def test_missing_or_invalid_peer() -> None:
    assert get_client_ip(request_from(None)) == UNKNOWN_IP
    assert get_client_ip(request_from("not-an-ip")) == UNKNOWN_IP


def test_deprecated_flag_keeps_old_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", [])
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", True)
    assert get_client_ip(request_from("203.0.113.9", "198.51.100.1, 10.0.0.5")) == "198.51.100.1"
    assert get_client_ip(request_from("203.0.113.9", "garbage")) == "203.0.113.9"
    assert "deprecated" in client_ip_policy()


def test_list_wins_over_deprecated_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", [NGINX])
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", True)
    assert get_client_ip(request_from("203.0.113.9", "198.51.100.1")) == "203.0.113.9"


# ---------------------------------------------------------------------------- trusted proxies


@pytest.mark.usefixtures("trusted")
def test_untrusted_peer_spoofing_xff_is_ignored() -> None:
    # The mobile app (or an attacker) calling the API directly.
    assert get_client_ip(request_from("203.0.113.9", "1.2.3.4")) == "203.0.113.9"
    assert get_client_ip(request_from("203.0.113.9", f"1.2.3.4, {NGINX}")) == "203.0.113.9"


@pytest.mark.usefixtures("trusted")
def test_trusted_peer_single_hop() -> None:
    assert get_client_ip(request_from(NGINX, "198.51.100.7")) == "198.51.100.7"
    assert get_client_ip(request_from(NGINX)) == NGINX


@pytest.mark.usefixtures("trusted")
def test_trusted_chain_is_skipped_right_to_left() -> None:
    # Browser -> website server -> Nginx -> API.
    assert get_client_ip(request_from(NGINX, f"198.51.100.7, {WEB}")) == "198.51.100.7"


@pytest.mark.usefixtures("trusted")
def test_mixed_chain_client_prepends_fake_entries() -> None:
    # Spoofed entries left of the first untrusted hop never win.
    assert get_client_ip(request_from(NGINX, f"1.1.1.1, 2.2.2.2, 198.51.100.7, {WEB}")) == "198.51.100.7"
    # Several headers are one list.
    assert get_client_ip(request_from(NGINX, "1.1.1.1", f"198.51.100.7, {WEB}")) == "198.51.100.7"


@pytest.mark.usefixtures("trusted")
def test_all_trusted_uses_leftmost() -> None:
    assert get_client_ip(request_from(NGINX, f"{WEB}, {NGINX}")) == WEB


@pytest.mark.usefixtures("trusted")
def test_invalid_entries_are_ignored() -> None:
    assert get_client_ip(request_from(NGINX, f"198.51.100.7, garbage, , 999.1.1.1, {WEB}")) == "198.51.100.7"
    assert get_client_ip(request_from(NGINX, "unknown")) == NGINX


@pytest.mark.usefixtures("trusted")
def test_ipv6_and_cidr() -> None:
    assert get_client_ip(request_from("fd00:1::2", "2001:db8::42")) == "2001:db8::42"
    assert get_client_ip(request_from("fd00:2::2", "2001:db8::42")) == "fd00:2::2"
    assert get_client_ip(request_from(NGINX, "2001:db8::42, fd00:1::9")) == "2001:db8::42"
    # IPv4-mapped IPv6 peers count as their IPv4 address.
    assert get_client_ip(request_from(f"::ffff:{NGINX}", "198.51.100.7")) == "198.51.100.7"


# ----------------------------------------------------------------------------------- settings


def test_setting_parses_ips_and_cidrs() -> None:
    parsed = make_settings(TRUSTED_PROXY_IPS=" 127.0.0.1, ::1 ,10.0.0.0/8,")
    assert parsed.TRUSTED_PROXY_IPS == ["127.0.0.1", "::1", "10.0.0.0/8"]
    assert make_settings().TRUSTED_PROXY_IPS == []


def test_invalid_setting_refuses_to_start() -> None:
    with pytest.raises(ValidationError, match="TRUSTED_PROXY_IPS: 'nginx.local'"):
        make_settings(TRUSTED_PROXY_IPS="127.0.0.1,nginx.local")
