"""HTTP endpoint policy (03-T3): allowed prefixes, plaintext rules, URL hygiene."""

from __future__ import annotations

import pytest

from aibench.core.errors import PolicyError
from aibench.security.endpoints import EndpointPolicy, origin_of


def test_default_origin_policy_allows_any_path_on_the_same_origin_only() -> None:
    policy = EndpointPolicy([origin_of("http://127.0.0.1:8765/answer")], allow_plaintext_http=False)
    policy.check("http://127.0.0.1:8765/answer")
    policy.check("http://127.0.0.1:8765/health?x=1")
    for url in (
        "http://127.0.0.1:8766/answer",  # other port
        "https://127.0.0.1:8765/answer",  # other scheme
        "http://localhost:8765/answer",  # other host spelling is another origin
    ):
        with pytest.raises(PolicyError):
            policy.check(url)


def test_path_prefix_matches_on_segment_boundaries() -> None:
    policy = EndpointPolicy(["https://api.example.com/v1/"], allow_plaintext_http=False)
    policy.check("https://api.example.com/v1")
    policy.check("https://api.example.com/v1/chat")
    policy.check("https://API.example.com:443/v1/chat")  # host case and default port
    with pytest.raises(PolicyError):
        policy.check("https://api.example.com/v10/chat")
    with pytest.raises(PolicyError):
        policy.check("https://api.example.com/admin")


def test_plaintext_http_is_loopback_only_unless_explicitly_allowed() -> None:
    strict = EndpointPolicy(["http://app.internal/"], allow_plaintext_http=False)
    with pytest.raises(PolicyError, match="allow_plaintext_http"):
        strict.check("http://app.internal/run")
    EndpointPolicy(["http://app.internal/"], allow_plaintext_http=True).check(
        "http://app.internal/run"
    )
    EndpointPolicy(["http://[::1]:9000/"], allow_plaintext_http=False).check("http://[::1]:9000/x")


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pass@127.0.0.1:8765/answer",  # credentials in URL
        "http://127.0.0.1:8765/v1/../admin",  # dot segment
        "http://127.0.0.1:8765/v1/%2e%2e/admin",  # encoded dot segment
        "file:///etc/passwd",
        "ftp://127.0.0.1/",
        "http://:80/",
    ],
)
def test_unsafe_urls_are_refused(url: str) -> None:
    policy = EndpointPolicy(["http://127.0.0.1:8765/"], allow_plaintext_http=True)
    with pytest.raises(PolicyError):
        policy.check(url)
