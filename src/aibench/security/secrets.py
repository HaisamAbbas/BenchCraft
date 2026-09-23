"""Secret-reference resolution and redaction (§16: "Scope credentials separately for
application and evaluators; pass only required secrets").

Only the `env:` source is implemented. Any other source (e.g. `keyring:`) is rejected with an
explicit error rather than silently resolving to nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping

from aibench.core.errors import ConfigError

SUPPORTED_SOURCES = ("env",)
# Values shorter than this are not scrubbed from captures: replacing e.g. "1" everywhere
# would corrupt output without protecting anything meaningful.
MIN_REDACTABLE_LENGTH = 4


def resolve_secret(ref: str, env: Mapping[str, str]) -> str:
    source, _, name = ref.partition(":")
    if source not in SUPPORTED_SOURCES:
        raise ConfigError(
            f"secret source {source!r} in {ref!r} is not supported; supported sources: "
            + ", ".join(SUPPORTED_SOURCES)
        )
    value = env.get(name)
    if value is None or value == "":
        raise ConfigError(f"secret {ref!r} is not set in the environment")
    return value


class Redactor:
    """Replaces resolved secret values with `<redacted:REF>` in captured bytes and text, so
    an application that echoes its credential never causes it to be persisted. Both the
    raw value and its JSON-escaped form are matched. Limits: secrets shorter than
    `MIN_REDACTABLE_LENGTH`, or re-encoded by the application (e.g. base64), are not found."""

    def __init__(self, secrets: Iterable[tuple[str, str]] = ()) -> None:
        forms: dict[str, str] = {}
        for ref, value in secrets:
            if len(value) < MIN_REDACTABLE_LENGTH:
                continue
            forms[value] = ref
            forms[json.dumps(value, ensure_ascii=False)[1:-1]] = ref
            forms[json.dumps(value)[1:-1]] = ref
        # Longest first so a secret that contains another is replaced whole.
        self._pairs = sorted(forms.items(), key=lambda pair: len(pair[0]), reverse=True)

    def text(self, value: str) -> str:
        for secret, ref in self._pairs:
            value = value.replace(secret, f"<redacted:{ref}>")
        return value

    def data(self, value: bytes, *, truncated: bool = False) -> bytes:
        """With `truncated=True`, also scrub a trailing partial secret cut off by a size
        cap (at least `MIN_REDACTABLE_LENGTH` characters of it)."""
        for secret, ref in self._pairs:
            value = value.replace(secret.encode("utf-8"), f"<redacted:{ref}>".encode())
        if truncated:
            for secret, ref in self._pairs:
                raw = secret.encode("utf-8")
                for size in range(len(raw) - 1, MIN_REDACTABLE_LENGTH - 1, -1):
                    if value.endswith(raw[:size]):
                        value = value[:-size] + f"<redacted-partial:{ref}>".encode()
                        break
        return value

    def text_stream(self, emit: Callable[[str], None]) -> TextRedactionStream:
        """Create a fragment sink that can redact secrets split across chunks."""
        return TextRedactionStream(self, emit)


class TextRedactionStream:
    """Redact streamed text without emitting a possible partial secret too early."""

    def __init__(self, redactor: Redactor, emit: Callable[[str], None]) -> None:
        self._redactor = redactor
        self._emit = emit
        self._pending = ""
        self._hold = max((len(secret) for secret, _ in redactor._pairs), default=1) - 1

    def feed(self, text: str, *, final: bool = False) -> None:
        self._pending += text
        safe_length = len(self._pending) if final else max(0, len(self._pending) - self._hold)
        if not final and safe_length:
            # A complete secret can straddle the nominal boundary. Keep the whole match
            # buffered so the prefix cannot leak before the next fragment arrives.
            for secret, _ in self._redactor._pairs:
                start = self._pending.find(secret)
                while start >= 0:
                    end = start + len(secret)
                    if start < safe_length < end:
                        safe_length = start
                    start = self._pending.find(secret, start + 1)
        if safe_length:
            ready, self._pending = self._pending[:safe_length], self._pending[safe_length:]
            self._emit(self._redactor.text(ready))
        if final and self._pending:
            ready, self._pending = self._pending, ""
            self._emit(self._redactor.text(ready))
