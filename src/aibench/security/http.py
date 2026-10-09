"""Bounded reads for HTTPX responses, including compressed content."""

from __future__ import annotations

import zlib
from collections.abc import Iterator
from typing import Any

import httpx

_READ_CHUNK_BYTES = 64 * 1024
# Only advertise compression formats this reader can decode with an output cap.
# HTTPX's default also advertises optional Brotli/Zstandard codecs, whose common
# Python APIs do not provide the bounded output needed here.
BOUNDED_ACCEPT_ENCODING = "gzip, deflate"


class ResponseTooLarge(ValueError):
    """The decoded HTTP response exceeds its configured byte limit."""

    def __init__(self, max_bytes: int, *, partial: bytes = b"") -> None:
        super().__init__(f"response exceeds {max_bytes} bytes")
        self.max_bytes = max_bytes
        self.partial = partial


class ResponseEncodingError(ValueError):
    """The response uses an unsupported or invalid content encoding."""


class _ZlibDecoder:
    def __init__(self, encoding: str) -> None:
        self.encoding = encoding
        self.first_attempt = True
        self.decompressor = self._new_decompressor(raw=False)

    def _new_decompressor(self, *, raw: bool) -> Any:
        if self.encoding in ("gzip", "x-gzip"):
            return zlib.decompressobj(zlib.MAX_WBITS | 16)
        wbits = -zlib.MAX_WBITS if raw else zlib.MAX_WBITS
        return zlib.decompressobj(wbits)

    def decode(self, data: bytes, max_output: int) -> bytes:
        try:
            output = self.decompressor.decompress(data, max_output)
        except zlib.error as exc:
            if self.encoding != "deflate" or not self.first_attempt:
                raise ResponseEncodingError("invalid compressed HTTP response") from exc
            self.decompressor = self._new_decompressor(raw=True)
            try:
                output = self.decompressor.decompress(data, max_output)
            except zlib.error as raw_exc:
                raise ResponseEncodingError("invalid compressed HTTP response") from raw_exc
        self.first_attempt = False
        return output

    @property
    def finished(self) -> bool:
        return self.decompressor.eof


def iter_limited_response(
    response: httpx.Response, max_bytes: int, *, chunk_size: int | None = _READ_CHUNK_BYTES
) -> Iterator[bytes]:
    """Yield decoded response chunks, stopping at the first byte beyond the limit.

    Decompression itself uses zlib's `max_length`, so an expansion bomb cannot first create
    an arbitrarily large decoded chunk. Call this inside the response streaming context so
    rejection closes the response. `chunk_size=None` preserves low-latency SSE delivery.
    """
    if max_bytes < 0:
        raise ValueError("max_bytes must not be negative")
    read_size = None if chunk_size is None else min(chunk_size, max_bytes + 1)
    encodings = [
        encoding.strip().lower()
        for encoding in response.headers.get("content-encoding", "").split(",")
        if encoding.strip() and encoding.strip().lower() != "identity"
    ]
    decoders: list[_ZlibDecoder] = []
    for encoding in reversed(encodings):
        if encoding not in ("gzip", "x-gzip", "deflate"):
            raise ResponseEncodingError(f"unsupported HTTP content encoding: {encoding}")
        decoders.append(_ZlibDecoder(encoding))

    raw_received = 0
    stage_received = [0] * len(decoders)
    decoded_received = 0
    if response.is_stream_consumed:
        chunks = iter((response.content,))
    elif read_size is None:
        chunks = response.iter_raw()
    else:
        chunks = response.iter_raw(chunk_size=read_size)
    for raw in chunks:
        raw_received += len(raw)
        raw_over_limit = raw_received > max_bytes
        if raw_over_limit and not decoders:
            remaining = max_bytes - decoded_received
            if remaining:
                yield raw[:remaining]
            raise ResponseTooLarge(max_bytes)
        decoded = raw
        for index, decoder in enumerate(decoders):
            remaining = max_bytes - stage_received[index]
            decoded = decoder.decode(decoded, remaining + 1)
            stage_received[index] += len(decoded)
            if stage_received[index] > max_bytes:
                if index == len(decoders) - 1 and remaining:
                    yield decoded[:remaining]
                raise ResponseTooLarge(max_bytes)
        remaining = max_bytes - decoded_received
        if len(decoded) > remaining:
            if remaining:
                yield decoded[:remaining]
            raise ResponseTooLarge(max_bytes)
        decoded_received += len(decoded)
        if decoded:
            yield decoded
        if raw_over_limit:
            raise ResponseTooLarge(max_bytes)

    if any(not decoder.finished for decoder in decoders):
        raise ResponseEncodingError("incomplete compressed HTTP response")


def read_limited_response(response: httpx.Response, max_bytes: int) -> bytes:
    """Read a complete decoded response only when it fits within the configured limit."""
    chunks: list[bytes] = []
    try:
        chunks.extend(iter_limited_response(response, max_bytes))
    except ResponseTooLarge as exc:
        partial = b"".join(chunks)
        raise ResponseTooLarge(max_bytes, partial=partial) from exc
    return b"".join(chunks)
