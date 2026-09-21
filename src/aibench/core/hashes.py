"""Canonical content hashing. No framework dependencies."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
    """Deterministic JSON encoding: sorted keys, no extraneous whitespace, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def content_hash(value: Any) -> str:
    """SHA-256 hex digest of the canonical JSON encoding of value, prefixed for clarity."""
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def bytes_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()
