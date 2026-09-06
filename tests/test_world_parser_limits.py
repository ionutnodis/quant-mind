"""Bounded hostile parsing and the asynchronous provider lifecycle."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime

import httpx
import pytest

from quantmind.world.models import WorldConfig
from quantmind.world.providers import ProviderError, fetch_source, parse_json, parse_xml
from quantmind.world.sources import SOURCES


NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
FEED = b"<rss><channel><item><title>Good</title><link>https://example.gov/good</link></item></channel></rss>"


@pytest.mark.parametrize("body", [
    pytest.param(b"<rss><channel>" + b"<item>" * 200 + b"</item>" * 200 + b"</channel></rss>", id="deep"),
    pytest.param(b"<rss><channel>" + b"<x/>" * 250_000 + b"</channel></rss>", id="wide"),
])
def test_xml_rejects_excessive_structure_below_the_download_cap(body) -> None:
    """A small compressed-looking tree cannot buy unlimited node traversal."""
    with pytest.raises(ProviderError, match="complex"):
        parse_xml(SOURCES[0], body, NOW)


@pytest.mark.parametrize("parser,kind,body", [
    (parse_xml, "rss", b"<rss><channel/></rss>"),
    (parse_json, "gdelt", b'{"articles":[]}'),
])
def test_direct_parser_call_keeps_the_body_work_bound(parser, kind, body) -> None:
    """The work bound also applies to non-network parser callers."""
    from dataclasses import replace

    with pytest.raises(ProviderError, match="too large"):
        parser(replace(SOURCES[0], kind=kind), body + b" " * (2 * 1024 * 1024), NOW)


def test_excessively_nested_json_has_a_safe_provider_error() -> None:
    from dataclasses import replace

    with pytest.raises(ProviderError, match="Invalid JSON feed"):
        parse_json(replace(SOURCES[0], kind="gdelt"), b'{"articles":' + b"[" * 10_000 + b"]" * 10_000 + b"}", NOW)


@pytest.mark.parametrize("kind", ["rss", "gdelt"])
def test_oversized_html_field_is_rejected_before_sanitizing_but_neighbor_survives(kind) -> None:
    """A record's markup work cannot consume the complete two-megabyte body allowance."""
    from dataclasses import replace

    oversized = "<b>" * 40_000 + "Unbounded"
    if kind == "rss":
        body = ("<rss><channel><item><title><![CDATA[" + oversized + "]]></title>"
                "<link>https://example.gov/huge</link></item>"
                "<item><title>Good</title><link>https://example.gov/good</link></item></channel></rss>").encode()
        events = parse_xml(SOURCES[0], body, NOW)
    else:
        body = json.dumps({"articles": [
            {"title": oversized, "url": "https://example.gov/huge"},
            {"title": "Good", "url": "https://example.gov/good"},
        ]}).encode()
        events = parse_json(replace(SOURCES[0], kind=kind), body, NOW)
    assert [event.title for event in events] == ["Good"]


async def test_hostile_nested_feed_does_not_starve_unrelated_event_loop_work() -> None:
    """The prior repeated subtree scans delayed a 10ms callback by over a second."""
    body = (b"<rss><channel>" + b"<item>" * 200 + b"<x/>" * 25_000
            + b"<title>Policy</title><link>https://example.gov/policy</link>"
            + b"</item>" * 200 + b"</channel></rss>")
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, content=body),
    )) as client:
        pending = asyncio.create_task(fetch_source(SOURCES[0], client, WorldConfig(), NOW))
        started = asyncio.get_running_loop().time()
        await asyncio.sleep(0.01)
        elapsed = asyncio.get_running_loop().time() - started
        result = (await asyncio.gather(pending, return_exceptions=True))[0]
    assert elapsed < 0.25
    assert isinstance(result, ProviderError)


@pytest.mark.parametrize("kind", ["rss", "gdelt"])
async def test_fetch_runs_parser_off_loop_and_drains_worker_before_timeout(monkeypatch, kind) -> None:
    """Timeout returns only after the bounded parser finishes, never abandoning its thread."""
    from dataclasses import replace
    import quantmind.world.providers as providers

    parser_name = "parse_xml" if kind == "rss" else "parse_json"
    original_parse = getattr(providers, parser_name)
    finished = threading.Event()

    def slow_parse(*args):
        time.sleep(0.2)  # deterministic CPU-stage latency, not an external request
        try:
            return original_parse(*args)
        finally:
            finished.set()

    monkeypatch.setattr(providers, parser_name, slow_parse)
    monkeypatch.setattr(providers, "WHOLE_REQUEST_TIMEOUT", 0.05)
    body = FEED if kind == "rss" else b'{"articles":[{"title":"Good","url":"https://example.gov/good"}]}'
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, content=body),
    )) as client:
        pending = asyncio.create_task(fetch_source(replace(SOURCES[0], kind=kind), client, WorldConfig(), NOW))
        started = asyncio.get_running_loop().time()
        await asyncio.sleep(0.01)
        elapsed = asyncio.get_running_loop().time() - started
        result = (await asyncio.gather(pending, return_exceptions=True))[0]
    assert elapsed < 0.1
    assert isinstance(result, ProviderError) and "timed out" in str(result)
    assert finished.is_set()


async def test_repeated_cancellation_cannot_abandon_an_active_parser(monkeypatch) -> None:
    """A second cancellation during cleanup cannot free capacity while work remains."""
    import quantmind.world.providers as providers

    original_parse = providers.parse_xml
    entered = threading.Event()
    finished = threading.Event()

    def slow_parse(*args):
        entered.set()
        time.sleep(0.2)
        try:
            return original_parse(*args)
        finally:
            finished.set()

    monkeypatch.setattr(providers, "parse_xml", slow_parse)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, content=FEED),
    )) as client:
        pending = asyncio.create_task(fetch_source(SOURCES[0], client, WorldConfig(), NOW))
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        pending.cancel()
        await asyncio.sleep(0.01)
        pending.cancel()
        result = (await asyncio.gather(pending, return_exceptions=True))[0]
    assert isinstance(result, asyncio.CancelledError)
    assert finished.is_set()
