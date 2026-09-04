"""Small HTTP trust-boundary helpers shared by external-data adapters."""

from __future__ import annotations


class ExternalPayloadTooLarge(ValueError):
    """An upstream response exceeded the adapter's documented memory bound."""


def read_bounded_text(
    response,
    *,
    max_bytes: int,
    encoding: str = "utf-8",
    errors: str = "strict",
) -> str:
    """Decode at most ``max_bytes`` from an HTTP response, failing closed."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except (TypeError, ValueError):
            declared = None
        if declared is not None and declared > max_bytes:
            raise ExternalPayloadTooLarge(
                f"upstream declared {declared} bytes; limit is {max_bytes}"
            )
    payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ExternalPayloadTooLarge(
            f"upstream response exceeded the {max_bytes}-byte limit"
        )
    return payload.decode(encoding, errors=errors)
