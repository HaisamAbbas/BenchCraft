"""HTTP endpoint policy (§7 "allowed endpoints", §16).

Every URL the HTTP runner contacts — the target, healthcheck/reset URLs and each redirect
hop — must match an allowed prefix (same scheme, host and port; path at a segment
boundary). URLs carrying credentials or dot segments are refused, and plain `http://` is
permitted only for loopback hosts unless explicitly allowed.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from aibench.core.errors import PolicyError

_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class _Endpoint:
    scheme: str
    host: str
    port: int
    path: str


def _parse(url: str) -> _Endpoint:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise PolicyError(f"malformed URL {url!r}: {exc}") from exc
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise PolicyError(f"URL scheme must be http or https: {url!r}")
    if parts.username is not None or parts.password is not None:
        raise PolicyError("URLs must not embed credentials; use secret_headers instead")
    host = (parts.hostname or "").lower()
    if not host:
        raise PolicyError(f"URL has no host: {url!r}")
    segments = unquote(parts.path).split("/")
    if any(segment in (".", "..") for segment in segments):
        raise PolicyError(f"URL path contains dot segments: {url!r}")
    return _Endpoint(scheme, host, port or _DEFAULT_PORTS[scheme], parts.path or "/")


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def origin_of(url: str) -> str:
    endpoint = _parse(url)
    host = f"[{endpoint.host}]" if ":" in endpoint.host else endpoint.host
    return f"{endpoint.scheme}://{host}:{endpoint.port}/"


class EndpointPolicy:
    def __init__(self, allowed: Iterable[str], *, allow_plaintext_http: bool) -> None:
        self.allowed = tuple(_parse(prefix) for prefix in allowed)
        if not self.allowed:
            raise PolicyError("an endpoint policy needs at least one allowed endpoint")
        self.allow_plaintext_http = allow_plaintext_http

    def check(self, url: str) -> None:
        endpoint = _parse(url)
        if (
            endpoint.scheme == "http"
            and not self.allow_plaintext_http
            and not is_loopback(endpoint.host)
        ):
            raise PolicyError(
                f"plain http to non-loopback host {endpoint.host!r} requires "
                "allow_plaintext_http: true"
            )
        if not any(self._matches(endpoint, prefix) for prefix in self.allowed):
            raise PolicyError(f"URL {url!r} is outside the allowed endpoints")

    @staticmethod
    def _matches(endpoint: _Endpoint, prefix: _Endpoint) -> bool:
        if (endpoint.scheme, endpoint.host, endpoint.port) != (
            prefix.scheme,
            prefix.host,
            prefix.port,
        ):
            return False
        base = prefix.path.rstrip("/")
        return not base or endpoint.path == base or endpoint.path.startswith(base + "/")
