from __future__ import annotations

import io

import pytest

from quantmind.sources.http import ExternalPayloadTooLarge, read_bounded_text


class _Response:
    def __init__(self, payload: bytes, content_length: str | None = None):
        self._body = io.BytesIO(payload)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def read(self, limit: int) -> bytes:
        return self._body.read(limit)


def test_bounded_http_reader_accepts_a_response_within_the_limit():
    response = _Response(b"market data", content_length="11")

    assert read_bounded_text(response, max_bytes=11) == "market data"


def test_bounded_http_reader_rejects_an_oversized_declared_length_without_reading():
    response = _Response(b"small body", content_length="1000")

    with pytest.raises(ExternalPayloadTooLarge, match="declared"):
        read_bounded_text(response, max_bytes=100)


def test_bounded_http_reader_rejects_streamed_bytes_beyond_the_limit():
    response = _Response(b"x" * 101)

    with pytest.raises(ExternalPayloadTooLarge, match="exceeded"):
        read_bounded_text(response, max_bytes=100)
