"""Shared validation and canonicalization for untrusted event links."""
from __future__ import annotations

import ipaddress
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def validate_public_http_url(value: str) -> str:
    """Return an unchanged public HTTP(S) URL, or raise ``ValueError``."""
    if not value or len(value) > 2048 or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("url must be a public HTTP(S) URL")
    try:
        parsed = urlsplit(value)
        # Accessing port performs urllib's malformed/out-of-range validation.
        parsed.port
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local")):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or host.isdigit():
                raise ValueError
        else:
            if not address.is_global:
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
