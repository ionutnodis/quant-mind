from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from quantmind.config import Settings
from quantmind.world.sources import SOURCES


NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
FEED = b"<rss><channel><item><title>Policy update</title><link>https://example.org/a</link><pubDate>Sat, 05 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>"


@pytest.mark.asyncio
async def test_once_uses_real_cache_and_selected_service_sources(tmp_path, capsys) -> None:
    from quantmind.world_cli import run

    requested: list[str] = []
    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.host or "")
        return httpx.Response(200, content=FEED)

    code = await run(["--source", "fed", "--source", "ecb"], settings=Settings(data_dir=tmp_path), transport=httpx.MockTransport(handler), clock=lambda: NOW)
    assert code == 0
    assert requested == ["www.federalreserve.gov", "www.ecb.europa.eu"]
    output = json.loads(capsys.readouterr().out)
    assert output == {"at": "2026-09-05T12:00:00Z", "result": {"updated": 2, "failed": 0, "skipped": 0}, "sources": {"ok": 2}}
    assert (tmp_path / "world.sqlite3").exists()


@pytest.mark.asyncio
async def test_all_selected_failures_exit_nonzero_but_partial_failure_is_zero(tmp_path, capsys) -> None:
    from quantmind.world_cli import run

    async def all_bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)
    code = await run(["--source", "fed"], settings=Settings(data_dir=tmp_path / "all"), transport=httpx.MockTransport(all_bad), clock=lambda: NOW)
    assert code == 1
    assert json.loads(capsys.readouterr().out)["result"]["failed"] == 1

    async def partial(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=FEED) if request.url.host == "www.federalreserve.gov" else httpx.Response(503)
    code = await run(["--source", "fed", "--source", "ecb"], settings=Settings(data_dir=tmp_path / "partial"), transport=httpx.MockTransport(partial), clock=lambda: NOW)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["result"] == {"updated": 1, "failed": 1, "skipped": 0}


@pytest.mark.asyncio
async def test_disabled_sources_report_skipped_without_claiming_success(tmp_path, capsys) -> None:
    from quantmind.world_cli import run

    code = await run(["--source", "x"], settings=Settings(data_dir=tmp_path), transport=httpx.MockTransport(lambda request: pytest.fail("disabled X made request")), clock=lambda: NOW)
    assert code == 0
    captured = capsys.readouterr()
    assert "success" not in captured.out.lower()
    assert json.loads(captured.out)["result"] == {"updated": 0, "failed": 0, "skipped": 1}


@pytest.mark.asyncio
async def test_x_paid_warning_contains_no_token_or_query(tmp_path, capsys) -> None:
    from quantmind.world_cli import run

    settings = Settings(data_dir=tmp_path, world_x_enabled=True, world_x_bearer_token="top-secret", world_x_query="private-query")
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})
    await run(["--source", "x"], settings=settings, transport=httpx.MockTransport(handler), clock=lambda: NOW)
    captured = capsys.readouterr()
    assert "paid" in captured.err.lower()
    assert "top-secret" not in captured.err + captured.out
    assert "private-query" not in captured.err + captured.out


@pytest.mark.asyncio
async def test_watch_uses_fake_sleep_and_shutdown_on_cancellation(tmp_path, monkeypatch, capsys) -> None:
    import quantmind.world_cli as cli

    calls = 0
    shutdown = False
    class FakeService:
        sources = SOURCES[:1]
        async def refresh(self):
            nonlocal calls
            calls += 1
            return {"updated": 1, "failed": 0, "skipped": 0}
        def snapshot(self, symbols, book_ref):
            return {"sources": [{"state": "ok"}]}
        async def shutdown(self):
            nonlocal shutdown
            shutdown = True
    async def fake_sleep(seconds: float):
        assert seconds == 300
        raise asyncio.CancelledError
    monkeypatch.setattr(cli.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await cli.run(["--watch", "--interval", "300"], settings=Settings(data_dir=tmp_path), service_factory=lambda *args, **kwargs: FakeService(), clock=lambda: NOW)
    assert calls == 1
    assert shutdown


@pytest.mark.parametrize("args", [["--source", "https://evil.test/rss"], ["--source", "unknown"], ["--watch", "--interval", "299"]])
def test_parser_rejects_non_registry_source_and_short_watch_interval(args) -> None:
    from quantmind.world_cli import parse_args
    with pytest.raises(SystemExit) as caught:
        parse_args(args)
    assert caught.value.code == 2
