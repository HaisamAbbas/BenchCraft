"""Response limits stop consuming streamed bodies instead of slicing after a full read."""

from __future__ import annotations

import gzip
import zlib

import httpx
import pytest

from aibench.security.http import ResponseTooLarge, read_limited_response


class Chunked(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.consumed = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.consumed += len(chunk)
            yield chunk

    def close(self) -> None:
        pass


def test_response_limit_stops_a_chunked_body_after_the_threshold() -> None:
    stream = Chunked([b"x" * 100 for _ in range(10)])
    response = httpx.Response(200, stream=stream)

    with pytest.raises(ResponseTooLarge, match="exceeds 100 bytes"):
        read_limited_response(response, 100)

    assert 100 < stream.consumed < 1_000


def test_response_limit_counts_decompressed_bytes() -> None:
    stream = Chunked([gzip.compress(b"x" * 1_000)])
    response = httpx.Response(
        200,
        headers={"Content-Encoding": "gzip"},
        stream=stream,
    )

    with pytest.raises(ResponseTooLarge, match="exceeds 100 bytes") as error:
        read_limited_response(response, 100)

    assert len(error.value.partial) == 100


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate"])
def test_decompression_cap_applies_during_gzip_and_deflate_expansion(encoding: str) -> None:
    payload = b"x" * 1_000
    if encoding == "gzip":
        header, compressed = "gzip", gzip.compress(payload)
    elif encoding == "deflate":
        header, compressed = "deflate", zlib.compress(payload)
    else:
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        header = "deflate"
        compressed = compressor.compress(payload) + compressor.flush()
    response = httpx.Response(
        200,
        headers={"Content-Encoding": header},
        stream=Chunked([compressed]),
    )

    with pytest.raises(ResponseTooLarge) as error:
        read_limited_response(response, 100)

    assert len(error.value.partial) == 100


def test_response_at_the_limit_is_read_completely() -> None:
    response = httpx.Response(200, stream=Chunked([b"a" * 60, b"b" * 40]))

    assert read_limited_response(response, 100) == b"a" * 60 + b"b" * 40
