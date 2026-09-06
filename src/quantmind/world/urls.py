"""Shared validation and canonicalization for untrusted event links."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def validate_public_http_url(value: str) -> str:
    """Admit DNS-hosted article links, without fetching/resolving their targets.

    IP literals (even public ones) and ambiguous numeric/encoded hosts are not
    news origins. This avoids urllib/WHATWG disagreements over octal, shortened
    and encoded IPv4. Punycode DNS names and Unicode paths are supported.
    This syntactic guard is not DNS-rebinding protection for outbound requests.
    Keep the browser defense in ``web/src/lib/world-url.ts`` aligned.
    """
    if not value or len(value) > 2048 or "\\" in value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("url must be a public HTTP(S) URL")
    try:
        parsed = urlsplit(value)
        # Accessing port performs urllib's malformed/out-of-range validation.
        parsed.port
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or not parsed.netloc.isascii() or any(char in parsed.netloc for char in "@%")):
            raise ValueError
        host = parsed.hostname.removesuffix(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local")):
            raise ValueError
        labels = host.split(".")
        if (len(host) > 253 or len(labels) < 2
                or not re.fullmatch(r"[a-z][a-z0-9-]*", labels[-1])
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)):
            raise ValueError
    except ValueError:
        raise ValueError("url must be a public HTTP(S) URL") from None
    return value


def canonicalize_public_http_url(value: str) -> str | None:
    """Validate and remove fragments plus recognized tracking parameters."""
    try:
        validate_public_http_url(value)
        parsed = urlsplit(value)
        query = urlencode(
            [
                (key, item)
                for key, item in parse_qsl(parsed.query, keep_blank_values=True)
                if not key.lower().startswith("utm_")
                and key.lower() not in {"fbclid", "gclid"}
            ],
            doseq=True,
        )
        canonical = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
        validate_public_http_url(canonical)
        return canonical
    except ValueError:
        return None
